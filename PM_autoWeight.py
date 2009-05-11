import subprocess
import tempfile
import os
import os.path

import maya.cmds as cmds
import maya.mel as mel
import maya.OpenMaya as api
import maya.OpenMayaAnim as apiAnim

import PMP.pyUtils
import PMP.maya.fileUtils
import PMP.maya.rigging

DEBUG = False
KEEP_PINOC_INPUT_FILES = True

_PINOCCHIO_DIR = os.path.join(os.path.dirname(__file__))
_PINOCCHIO_BIN = os.path.join(_PINOCCHIO_DIR, 'AttachWeights.exe')

class DEFAULT_SKELETONS(object): pass
class HUMAN_SKELETON(DEFAULT_SKELETONS): pass

def pinocchioSkeletonExport(skeletonRoot, skelFile=None):
    """
    Exports the skeleton to a file format that pinocchio can understand.

    Returns (skelFile, skelList), where skelList is the list returned
    by  makePinocchioSkeletonList.
    """
    if skelFile is None:
        skelFile = PMP.maya.fileUtils.browseForFile(m=1, actionName='Export')
    skelList = makePinocchioSkeletonList(skeletonRoot)
    fileObj = open(skelFile, mode="w")
    try:
        for jointIndex, (joint, parentIndex) in enumerate(skelList):
            jointCoords = getTranslation(joint, space='world')
            fileObj.write("%d %.5f %.5f %.5f %d\r\n" % (jointIndex,
                                                        jointCoords[0],
                                                        jointCoords[1],
                                                        jointCoords[2],
                                                        parentIndex))
    finally:
        fileObj.close()
    return (skelFile, skelList)

def pinocchioObjExport(mesh, objFilePath):
    savedSel = cmds.ls(sl=1)
    try:
        if not isATypeOf(mesh, 'geometryShape'):
            subShape = getShape(mesh)
            if subShape:
                mesh = subShape
        if not isATypeOf(mesh, 'geometryShape'):
            raise TypeError('cannot find a geometry shape for %s' % mesh)
            
        meshDup = addShape(mesh)
        cmds.polyTriangulate(meshDup, ch=0)
        cmds.select(meshDup, r=1)
        cmds.file(objFilePath,
                op="groups=0;ptgroups=0;materials=0;smoothing=0;normals=0",
                typ="OBJexport", es=True, f=1)
        cmds.delete(meshDup)
    finally:
        cmds.select(savedSel)
    return objFilePath


def makePinocchioSkeletonList(rootJoint):
    """
    Given a joint, returns info used for the pinocchio skeleton export.
    
    Each item in the list is a tuple ([x,y,z], parentIndex), where
    parentIndex is an index into the list.
    """
    return _makePinocchioSkeletonList([], rootJoint, -1)

def _makePinocchioSkeletonList(skelList, newJoint, newJointParent):
    newIndex = len(skelList)
    skelList.append((newJoint, newJointParent))

    jointChildren = listForNone(cmds.listRelatives(newJoint, type="joint",
                                                   children=True,
                                                   noIntermediate=True))
    for joint in jointChildren:
        _makePinocchioSkeletonList(skelList, joint, newIndex)
    return skelList

def pinocchioWeightsImport(mesh, skin, skelList, weightFile=None):
    #Ensure that all influences in the skelList are influences for the skin
    allInfluences = influenceObjects(skin)
    pinocInfluences = [joint for joint, parent in skelList]
    for joint in pinocInfluences:
        if not nodeIn(joint, allInfluences):
            cmds.skinCluster(skin, edit=1, addInfluence=joint)

    if weightFile is None:
        weightFile = PMP.maya.fileUtils.browseForFile(m=0, actionName='Import')
    vertBoneWeights = readPinocchioWeights(weightFile)
    numVertices = len(vertBoneWeights)
    numBones = len(vertBoneWeights[0])
    numWeights = numVertices * numBones
    numJoints = len(skelList)
    if DEBUG:
        print "numVertices:", numVertices
        print "numBones:", numBones
    assert(numBones == numJoints - 1,
           "numBones (%d) != numJoints (%d) - 1" % (numBones, numJoints))

    # Pinocchio sets weights per-bone... maya weights per joint.
    # Need to decide whether to assign the bone weight to the 'start' joint
    #   of the bone, or the 'end' joint
    boneIndexToJointIndex = [0] * numBones
    vertJointWeights = [[0] * numJoints for i in xrange(numVertices)]

    assignBoneToEndJoint = False
    if assignBoneToEndJoint:
        for jointIndex in xrange(1, numJoints):
            boneIndexToJointIndex[jointIndex - 1] = jointIndex
    else:
        for jointIndex in xrange(1, numJoints):
            parentIndex = skelList[jointIndex][1]
            boneIndexToJointIndex[jointIndex - 1] = parentIndex
    
    for vertIndex, boneWeights in enumerate(vertBoneWeights):
        assert(abs(sum(boneWeights) - 1) < 0.1,
               "Output for vert %d not normalized - total was: %.03f" %
               (vertIndex, sum(boneWeights)))
        for boneIndex, boneValue in enumerate(boneWeights):
            # multiple bones can correspond to a single joint -
            # make sure to add the various bones values together!
            jointIndex = boneIndexToJointIndex[boneIndex] 
            vertJointWeights[vertIndex][jointIndex] += boneValue

    if DEBUG:
        print "vertJointWeights:"
        for i, jointWeights in enumerate(vertJointWeights):
            if i < 20:
                print jointWeights
            else:
                print "..."
                break
            
    # Zero all weights
    cmds.skinPercent(skin, mesh, pruneWeights=100, normalize=False)

    if confirmNonUndoableMethod():
        apiWeights = api.MDoubleArray(numWeights, 0)
        for vertIndex, jointWeights in enumerate(vertJointWeights):
            for jointIndex, jointValue in enumerate(jointWeights):
                apiWeights.set(jointValue, vertIndex * numBones + jointIndex)
        apiJointIndices = api.MIntArray(numBones, 0)
        for apiIndex, joint in enumerate(influenceObjects(skin)):
            apiJointIndices.set(apiIndex, getNodeIndex(joint, pinocInfluences))
        apiComponents = api.MFnSingleIndexedComponent().create(api.MFn.kMeshVertComponent)
        apiVertices = api.MIntArray(numVertices, 0)
        for i in xrange(numVertices):
            apiVertices.set(i, i)
        api.MFnSingleIndexedComponent(apiComponents).addElements(apiVertices) 
        mfnSkin = apiAnim.MFnSkinCluster(toMObject(skin))
        oldWeights = api.MDoubleArray()
        undoState = cmds.undoInfo(q=1, state=1)
        cmds.undoInfo(state=False)
        try:
            mfnSkin.setWeights(toMDagPath(mesh),
                               apiComponents,
                               apiJointIndices,
                               apiWeights,
                               False,
                               oldWeights)
        finally:
            cmds.flushUndo()
            cmds.undoInfo(state=undoState)
    else:
        cmds.progressWindow(title="Setting new weights...", isInterruptable=True,
                            max=numVertices)
        lastUpdateTime = cmds.timerX()
        updateInterval = .5
        for vertIndex, vertJoints in enumerate(vertJointWeights):
            jointValues = {}
            if cmds.progressWindow( query=True, isCancelled=True ) :
                break
            #print "weighting vert:", vertIndex
            for jointIndex, jointValue in enumerate(vertJoints):
                if jointValue > 0:
                    jointValues[pinocInfluences[jointIndex]] = jointValue
    
            if cmds.timerX(startTime=lastUpdateTime) > updateInterval:
                progress = vertIndex
                cmds.progressWindow(edit=True,
                                    progress=progress,
                                    status="Setting Vert: (%i of %i)" % (progress, numVertices))
                lastUpdateTime = cmds.timerX()

            cmds.skinPercent(skin, mesh.vtx[vertIndex], normalize=False,
                             transformValue=jointValues.items())
        cmds.progressWindow(endProgress=True)    

def confirmNonUndoableMethod():
    return True

def readPinocchioWeights(weightFile):
    weightList = []
    fileObj = open(weightFile)
    try:
        for line in fileObj:
            weightList.append([float(x) for x in line.strip().split(' ')])
    finally:
        fileObj.close()
    return weightList

def runPinocchioBin(meshFile, weightFile, fit=False):
    # Change current directory to ensure we know where attachment.out will be
    os.chdir(_PINOCCHIO_DIR)
    exeAndArgs = [_PINOCCHIO_BIN, meshFile, '-skel', weightFile]
    if fit:
        exeAndArgs.append('-fit')
    subprocess.check_call(exeAndArgs)

def autoWeight(rootJoint=None, mesh=None, skin=None, fit=False):
    if rootJoint is None or mesh is None:
        sel = cmds.ls(sl=1)
        if rootJoint is None:
            rootJoint = sel.pop(0)
        if mesh is None:
            mesh = sel[0]
    
    if skin is None:
        skinClusters = getSkinClusters(mesh)
        if skinClusters:
            skin = skinClusters[0]
        else:
            skin = cmds.skinCluster(mesh, rootJoint, rui=False)[0]
    
    tempArgs={}
    if KEEP_PINOC_INPUT_FILES:
        objFilePath = os.path.join(_PINOCCHIO_DIR, 'mayaToPinocModel.obj')
    else:
        objFileHandle, objFilePath = tempfile.mkstemp('.obj', **tempArgs)
        os.close(objFileHandle)
    try:
        if KEEP_PINOC_INPUT_FILES:
            skelFilePath = os.path.join(_PINOCCHIO_DIR, 'mayaToPinocSkel.skel')
        else:
            skelFileHandle, skelFilePath = tempfile.mkstemp('.skel',**tempArgs)
            os.close(skelFileHandle)
        try:
            skelFilePath, skelList = \
                pinocchioSkeletonExport(rootJoint, skelFilePath)
            objFilePath = pinocchioObjExport(mesh, objFilePath)
            
            runPinocchioBin(objFilePath, skelFilePath, fit=fit)
            pinocchioWeightsImport(mesh, skin, skelList,
                                   weightFile=os.path.join(_PINOCCHIO_DIR,
                                                           "attachment.out"))
        finally:
            if not KEEP_PINOC_INPUT_FILES and os.path.isfile(skelFilePath):
                os.remove(skelFilePath)
    finally:
        if not KEEP_PINOC_INPUT_FILES and os.path.isfile(objFilePath):
            os.remove(objFilePath)

# This doesn't work - apparently demoui can't take animation data for arbitrary
# skeletons - it requires exactly 114 entries per line??? 
#def exportPinocchioAnimation(skelList, filePath,
#                             startTime=None, endTime=None):
#    if startTime is None:
#        startTime = playbackOptions(q=1,  min=1)
#    if endTime is None:
#        endTime = playbackOptions(q=1,  max=1)
#
#    timeIncrement = playbackOptions(q=1, by=1)
#    
#    fileObj = open(filePath, mode='w')
#    try:
#        currentTime(startTime)
#        while currentTime() <= endTime:
#            for joint, parent in skelList:
#                for coord in getTranslation(joint, space='world'): 
#                    fileObj.write('%f ' % coord)
#            fileObj.write('\n')
#            currentTime(currentTime() + timeIncrement)
#    finally:
#        fileObj.close()

def nodeIn(node, nodeList):
    for compNode in nodeList:
        if isSameObject(node, compNode):
            return True
    else:
        return False
    
def getNodeIndex(node, nodeList):
    for i, compNode in enumerate(nodeList):
        if isSameObject(node, compNode):
            return i
    else:
        return None

def isSameObject(node1, node2):
    return mel.eval('isSameObject("%s", "%s")' % (node1, node2))
#==============================================================================
# Pymel Replacements
#==============================================================================

def listForNone( res ):
    if res is None:
        return []
    return res

def getTranslation(transform, **kwargs):
    space = kwargs.pop('space', None)
    if space == 'world':
        kwargs['worldSpace'] = True
    return cmds.xform(transform, q=1, translation=1)

def getShape( transform, **kwargs ):
    kwargs['shapes'] = True
    try:
        return getChildren(transform, **kwargs )[0]            
    except IndexError:
        pass

def getChildren(self, **kwargs ):
    kwargs['children'] = True
    kwargs.pop('c',None)
    return listForNone(cmds.listRelatives( self, **kwargs))

def getParent(transform, **kwargs):
    kwargs['parent'] = True
    kwargs.pop('p', None)
    return cmds.listRelatives( transform, **kwargs)[0]

def addShape( origShape, **kwargs ):
    """
    origShape will be duplicated and added under the existing parent transform
        (instead of duplicating the parent transform)
    """
    kwargs['returnRootsOnly'] = True
    kwargs.pop('rr', None)
    
    for invalidArg in ('renameChildren', 'rc', 'instanceLeaf', 'ilf',
                       'parentOnly', 'po', 'smartTransform', 'st'):
        if kwargs.get(invalidArg, False) :
            raise ValueError("addShape: argument %r may not be used with 'addShape' argument" % invalidArg)
    name=kwargs.pop('name', kwargs.pop('n', None))
                
    if 'shape' not in cmds.nodeType(origShape, inherited=True):
        raise TypeError('addShape argument to be a shape (%r)'
                        % origShape)

    # This is somewhat complex, because if we have a transform with
    # multiple shapes underneath it,
    #   a) The transform and all shapes are always duplicated
    #   b) After duplication, there is no reliable way to distinguish
    #         which shape is the duplicate of the one we WANTED to
    #         duplicate (cmds.shapeCompare does not work on all types
    #         of shapes - ie, subdivs)
    
    # To get around this, we:
    # 1) duplicate the transform ONLY (result: dupeTransform1)
    # 2) instance the shape we want under the new transform
    #    (result: dupeTransform1|instancedShape)
    # 3) duplicate the new transform
    #    (result: dupeTransform2, dupeTransform2|duplicatedShape)
    # 4) delete the transform with the instance (delete dupeTransform1)
    # 5) place an instance of the duplicated shape under the original
    #    transform (result: originalTransform|duplicatedShape)
    # 6) delete the extra transform (delete dupeTransform2)
    # 7) rename the final shape (if requested)
    
    # 1) duplicate the transform ONLY (result: dupeTransform1)
    dupeTransform1 = cmds.duplicate(origShape, parentOnly=1)[0]

    # 2) instance the shape we want under the new transform
    #    (result: dupeTransform1|instancedShape)
    cmds.parent(origShape, dupeTransform1, shape=True, addObject=True,
                relative=True)
    
    # 3) duplicate the new transform
    #    (result: dupeTransform2, dupeTransform2|duplicatedShape)
    dupeTransform2 = cmds.duplicate(dupeTransform1, **kwargs)[0]

    # 4) delete the transform with the instance (delete dupeTransform1)
    cmds.delete(dupeTransform1)

    # 5) place an instance of the duplicated shape under the original
    #    transform (result: originalTransform|duplicatedShape)
    newShape = cmds.parent(getShape(dupeTransform2),
                           getParent(origShape),
                           shape=True, addObject=True,
                           relative=True)[0]

    # 6) delete the extra transform (delete dupeTransform2)
    cmds.delete(dupeTransform2)
    
    # 7) rename the final shape (if requested)
    if name is not None:
        newShape = cmds.rename(newShape, name)
    
    cmds.select(newShape, r=1)
    return newShape

def influenceObjects(skinCluster):
    mfnSkin = apiAnim.MFnSkinCluster(toMObject(skinCluster))
    dagPaths = api.MDagPathArray()
    mfnSkin.influenceObjects(dagPaths)
    influences = []
    for i in xrange(dagPaths.length()):
        influences.append(dagPaths[i].fullPathName())
    return influences

def isValidMObject (obj):
    if isinstance(obj, api.MObject) :
        return not obj.isNull()
    else :
        return False

def toMObject (nodeName):
    """ Get the API MObject given the name of an existing node """ 
    sel = api.MSelectionList()
    obj = api.MObject()
    result = None
    try :
        sel.add( nodeName )
        sel.getDependNode( 0, obj )
        if isValidMObject(obj) :
            result = obj 
    except :
        pass
    return result

def toMDagPath (nodeName):
    """ Get an API MDagPAth to the node, given the name of an existing dag node """ 
    obj = toMObject (nodeName)
    if obj :
        dagFn = api.MFnDagNode (obj)
        dagPath = api.MDagPath()
        dagFn.getPath ( dagPath )
        return dagPath

#==============================================================================
# PM Scripts Replacements
#==============================================================================

def isATypeOf(node, type):
    """Returns true if node is of the given type, or inherits from it."""
    if isinstance(node, basestring) and cmds.objExists(node):
        return type in cmds.nodeType(node, inherited=True)
    else:
        return False
    
def getSkinClusters(mesh):
    """
    Returns a list of skinClusters attached the given mesh.
    """
    return [x for x in listForNone(cmds.listHistory(mesh))
            if isATypeOf(x, 'skinCluster')]
