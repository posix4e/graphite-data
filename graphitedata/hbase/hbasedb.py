import json
import fnmatch
import time
import struct

from thrift.transport import TSocket
from graphitedata.tsdb import Node, BranchNode
from graphitedata.hbase.ttypes import *
from graphitedata.hbase.Hbase import Client
from graphitedata import util



# we manage a namespace table (NS) and then a data table (data)

# the NS table is organized to mimic a tree structure, with a ROOT node containing links to its children.as
# Nodes are either a BRANCH node which contains multiple child columns prefixed with c_, or a LEAF node
# containing a single INFO column

# IDCTR
#   - unique id counter

# ROOT
#   - c_branch1 -> m_branch1
#   - c_leaf1 -> m_leaf1

# m_branch1
#   - c_leaf2 -> m_branch1.leaf2

# m_leaf1
#    - INFO -> info json

# m_branch1.leaf2
#    - INFO -> info json

# the INFO json on branch nodes contains graphite info plus an ID field, consisting of a 32bit int

# we then maintain a data table with keys that are a compound of metric ID + unix timestamp for 8 byte keys

dataKeyFmt = ">LL"
dataValFmt = ">Ld"

class ArchiveConfig:
    __slots__ = ('archiveId','secondsPerPoint','points')

    def __init__(self,tuple,id):
        self.secondsPerPoint,self.points = tuple
        self.archiveId = id

class HbaseTSDB:
    __slots__ = ('transport','client','metaTable','dataTable')

    def __init__(self, host,port,table_prefix):
        # set up client
        self.metaTable = table_prefix + "META"
        self.dataTable = table_prefix + "DATA"
        socket = TSocket.TSocket(host, port)
        self.transport = TTransport.TBufferedTransport(socket)
        protocol = TBinaryProtocol.TBinaryProtocol(self.transport)
        self.client = Client(protocol)
        self.transport.open()
        # ensure both our tables exist
        tables = self.client.getTableNames()
        if self.metaTable not in tables:
            self.client.createTable(self.metaTable,[ColumnDescriptor("cf:")])
            # add counter record
            self.client.atomicIncrement(self.metaTable,"CTR","cf:CTR",1)
        if self.dataTable not in tables:
            self.client.createTable(self.dataTable,[ColumnDescriptor("cf:")])


    # returns info for the underlying db (including 'aggregationMethod')

    # info returned in the format
    #info = {
    #  'aggregationMethod' : aggregationTypeToMethod.get(aggregationType, 'average'),
    #  'maxRetention' : maxRetention,
    #  'xFilesFactor' : xff,
    #  'archives' : archives,
    #}
    # where archives is a list of
    # archiveInfo = {
    #  'archiveId': unique id,
    #  'secondsPerPoint' : secondsPerPoint,
    #  'points' : points, number of points per
    #  'retention' : secondsPerPoint    * points,
    #  'size' : points * pointSize,
    #}
    #

    def info(self, metric):
        # info is stored as serialized map under META#METRIC
        key = "m_" + metric
        result = self.client.get(self.metaTable, "m_" + metric, "cf:INFO", None)
        if len(result) == 0:
            raise Exception("No metric " + metric)
        return json.loads(result[0].value)

    # aggregationMethod specifies the method to use when propogating data (see ``whisper.aggregationMethods``)
    # xFilesFactor specifies the fraction of data points in a propagation interval that must have known values for a propagation to occur.  If None, the existing xFilesFactor in path will not be changed
    def setAggregationMethod(self, metric, aggregationMethod, xFilesFactor=None):
        currInfo = self.info(metric)
        currInfo['aggregationMethod'] = aggregationMethod
        currInfo['xFilesFactor'] = xFilesFactor

        infoJson = json.dumps(currInfo)
        self.client.mutateRow(self.metaTable,"m_" + metric,[Mutation(column="cf:INFO",value=infoJson)],None)
        return


    # archiveList is a list of archives, each of which is of the form (secondsPerPoint,numberOfPoints)
    # xFilesFactor specifies the fraction of data points in a propagation interval that must have known values for a propagation to occur
    # aggregationMethod specifies the function to use when propogating data (see ``whisper.aggregationMethods``)
    def create(self, metric, archiveList, xFilesFactor, aggregationMethod, isSparse, doFallocate):

        #for a in archiveList:
        #    a['archiveId'] = (self.client.atomicIncrement(self.metaTable,"CTR","cf:CTR",1))

        archiveMapList = [
            {'archiveId': (self.client.atomicIncrement(self.metaTable,"CTR","cf:CTR",1)),
             'secondsPerPoint': a[0],
             'points': a[1],
             'retention': a[0] * a[1],
            }
            for a in archiveList
        ]
        #newId = self.client.atomicIncrement(self.metaTable,"CTR","cf:CTR",1)

        oldest = max([secondsPerPoint * points for secondsPerPoint,points in archiveList])
        # then write the metanode
        info = {
            'aggregationMethod' : aggregationMethod,
            'maxRetention' : oldest,
            'xFilesFactor' : xFilesFactor,
            'archives' : archiveMapList,
        }
        self.client.mutateRow(self.metaTable,"m_" + metric,[Mutation(column="cf:INFO",value=json.dumps(info))],None)
        # finally, ensure links exist
        metric_parts = metric.split('.')
        priorParts = ""
        for part in metric_parts:
            # if parent is empty, special case for root
            if priorParts == "":
                metricParentKey = "ROOT"
                metricKey = "m_" + part
                priorParts = part
            else:
                metricParentKey = "m_" + priorParts
                metricKey = "m_" + priorParts + "." + part
                priorParts += "." + part

            # make sure parent of this node exists and is linked to us
            parentLink = self.client.get(self.metaTable,metricParentKey,"cf:c_" + part,None)
            if len(parentLink) == 0:
                self.client.mutateRow(self.metaTable,metricParentKey,[Mutation(column="cf:c_"+part,value=metricKey)],None)


    # points is a list of (timestamp,value) points
    def update_many(self, metric, points):
        info = self.info(metric)
        now = int( time.time() )
        archives = iter( info['archives'] )
        currentArchive = archives.next()
        currentPoints = []

        for point in points:
            age = now - point[0]

            while currentArchive['retention'] < age: #we can't fit any more points in this archive
                if currentPoints: #commit all the points we've found that it can fit
                    currentPoints.reverse() #put points in chronological order
                    self.__archive_update_many(info,currentArchive,currentPoints)
                    currentPoints = []
                try:
                    currentArchive = archives.next()
                except StopIteration:
                    currentArchive = None
                    break

            if not currentArchive:
                break #drop remaining points that don't fit in the database

            currentPoints.append(point)

        if currentArchive and currentPoints: #don't forget to commit after we've checked all the archives
            currentPoints.reverse()
            self.__archive_update_many(info,currentArchive,currentPoints)

    def __archive_update_many(self,info,archive,points):
        numPoints = archive['points']
        step = archive['secondsPerPoint']
        archiveId = archive['archiveId']
        alignedPoints = [(timestamp - (timestamp % step), value)
                         for (timestamp,value) in points ]
        alignedPoints = dict(alignedPoints).items() # Take the last val of duplicates

        for timestamp,value in alignedPoints:
            slot = int((timestamp / step) % numPoints)
            print "putting timestamp " + timestamp.__str__() + " into slot " + slot.__str__()
            rowkey = struct.pack(dataKeyFmt,archiveId,slot)
            rowval = struct.pack(dataValFmt,timestamp,value)
            print("put rowkey: " + rowkey + " roval: " + rowval)
            self.client.mutateRow(self.dataTable,rowkey,[Mutation(column="cf:d",value=rowval)],None)

        #Now we propagate the updates to lower-precision archives
        higher = archive
        lowerArchives = [arc for arc in info['archives'] if arc['secondsPerPoint'] > archive['secondsPerPoint']]

        for lower in lowerArchives:
            fit = lambda i: i - (i % lower['secondsPerPoint'])
            lowerIntervals = [fit(p[0]) for p in alignedPoints]
            uniqueLowerIntervals = set(lowerIntervals)
            propagateFurther = False
            for interval in uniqueLowerIntervals:
                if self.__propagate(info, interval, higher, lower):
                    propagateFurther = True

            if not propagateFurther:
                break
            higher = lower

    def __propagate(self,info,timestamp,higher,lower):
        aggregationMethod = info['aggregationMethod']
        xff = info['xFilesFactor']

        # we want to update the items from higher between these two
        intervalStart = timestamp - (timestamp % lower['secondsPerPoint'])
        intervalEnd = intervalStart + lower['secondsPerPoint']

        higherResData = self.__archive_fetch(higher['archiveId'],intervalStart,intervalEnd)

        known_datapts = [v for v in higherResData if v is not None] # strip out "nones"
        if (len(known_datapts) / len(higherResData)) > xff: # we have enough data, so propagate downwards
            aggregateValue = util.aggregate(aggregationMethod,known_datapts)
            lowerSlot = timestamp / lower['secondsPerPoint'] % lower['numPoints']
            rowkey = struct.pack(dataKeyFmt,lower['archiveId'],lowerSlot)
            rowval = struct.pack(dataValFmt,timestamp,aggregateValue)
            print("put rowkey: " + rowkey + " roval: " + rowval)
            self.client.mutateRow(self.dataTable,rowkey,[Mutation(column="cf:d",value=rowval)],None)

    # returns list of values between the two times.  length is endTime - startTime / secondsPerPorint.
    # should be aligned with secondsPerPoint for proper results
    def __archive_fetch(self,archive,startTime,endTime):
        step = archive['secondsPerPoint']
        numPoints = archive['points']
        startTime = int(startTime - (startTime % step))
        endTime = int(endTime - (endTime % step))
        startSlot = int((startTime / step) % numPoints)
        endSlot = int((endTime / step) % numPoints)
        print "startTime " + startTime.__str__() + " endtime " + endTime.__str__()
        print "startSlot " + startSlot.__str__() + " end slot " + endSlot.__str__()
        if startSlot > endSlot: # we wrapped so make 2 queries
            ranges = [(0,endSlot+1),(startSlot,numPoints)]
        else:
            ranges = [(startSlot,endSlot+1)]

        print "ranges: " + ranges.__str__()
        for t in ranges:
            startkey = struct.pack(dataKeyFmt,archive['archiveId'],t[0])
            endkey = struct.pack(dataKeyFmt,archive['archiveId'],t[1])
            print "scanning startkey: " + startkey.__str__() + ", endkey: " + endkey
            scannerId = self.client.scannerOpenWithStop(self.dataTable,startkey,endkey,["cf:d"],None)

            numSlots = (endTime - startTime) / archive['secondsPerPoint']
            ret = [None] * numSlots

            for row in self.client.scannerGetList(scannerId,100000):
                print row.columns
                print "got row with data val " + row.columns["cf:d"].value
                (timestamp,value) = struct.unpack(dataValFmt,row.columns["cf:d"].value)
                if timestamp >= startTime and timestamp <= endTime:
                    returnslot = (timestamp - startTime) / archive['secondsPerPoint'] - 1
                    ret[returnslot] = value
            self.client.scannerClose(scannerId)
        return ret


    def exists(self,metric):
        return len(self.client.getRow(self.metaTable,"m_" + metric,None)) > 0


    # fromTime is an epoch time
    # untilTime is also an epoch time, but defaults to now.
    #
    # Returns a tuple of (timeInfo, valueList)
    # where timeInfo is itself a tuple of (fromTime, untilTime, step)
    # Returns None if no data can be returned
    def fetch(self,info,fromTime,untilTime):
        now = int( time.time() )
        if untilTime is None:
            untilTime = now
        fromTime = int(fromTime)
        untilTime = int(untilTime)
        if untilTime > now:
            untilTime = now
        if (fromTime > untilTime):
            raise Exception("Invalid time interval: from time '%s' is after until time '%s'" % (fromTime, untilTime))

        if fromTime > now:  # from time in the future
            return None
        oldestTime = now - info['maxRetention']
        if fromTime < oldestTime:
            fromTime = oldestTime
        # iterate archives to find the smallest
        diff = now - fromTime
        for archive in info['archives']:
            if archive['retention'] >= diff:
                break
        return self.__archive_fetch(archive,fromTime,untilTime)

    # returns [ start, end ] where start,end are unixtime ints
    def get_intervals(self,metric):
        pass

    # returns list of metrics as strings
    def find_nodes(self,query):
        # break query into parts
        clean_pattern = query.pattern.replace('\\', '')
        pattern_parts = clean_pattern.split('.')

        return self._find_paths("ROOT",pattern_parts)

    def _find_paths(self, currNodeRowKey, patterns):
        """Recursively generates absolute paths whose components underneath current_node
        match the corresponding pattern in patterns"""
        pattern = patterns[0]
        patterns = patterns[1:]

        nodeRow = self.client.getRow(self.metaTable,currNodeRowKey,None)
        if len(nodeRow) == 0:
            return

        subnodes = {}
        for k,v in nodeRow[0].columns.items():
            if k.startswith("cf:c_"): # branches start with c_
                key = k.split("_",2)[1] # pop off cf:c_ prefix
                subnodes[key] = v.value

        matching_subnodes = match_entries(subnodes.keys(),pattern)

        #print "rowkey: " + currNodeRowKey + " matching subnodes:  " + matching_subnodes.__str__()
        if patterns: # we've still got more directories to traverse
            for subnode in matching_subnodes:
                rowKey = subnodes[subnode]
                subNodeContents = self.client.getRow(self.metaTable,rowKey,None)

                # leafs have a cf:INFO column describing their data
                # we can't possibly match on a leaf here because we have more components in the pattern,
                # so only recurse on branches
                if "cf:INFO" not in subNodeContents[0].columns:
                    for m in self._find_paths(rowKey,patterns):
                        yield m



        else: # at the end of the pattern
            for subnode in matching_subnodes:
                rowKey = subnodes[subnode]
                nodeRow = self.client.getRow(self.metaTable,rowKey,None)
                if len(nodeRow) == 0:
                    continue
                metric = rowKey.split("_",2)[1] # pop off "m_" in key
                if "cf:INFO" in nodeRow[0].columns:
                    info = json.loads(nodeRow[0].columns["cf:INFO"].value)
                    yield HbaseLeafNode(metric,info,self)
                else:
                    yield BranchNode(metric)

def NewHbaseTSDB(arg="localhost:9090:graphite_"):
    host,port,prefix = arg.split(":")
    return HbaseTSDB(host,port,prefix)

class HbaseLeafNode(Node):
    __slots__ = ('db', 'intervals','info')

    def __init__(self, path, info, hbasedb):
        Node.__init__(self, path)
        self.db = hbasedb
        self.info = info
        self.intervals = hbasedb.get_intervals(path)
        self.is_leaf = True

    def fetch(self, startTime, endTime):
        return self.db.fetch(self.info, startTime, endTime)

    def __repr__(self):
        return '<LeafNode[%x]: %s >' % (id(self), self.path)

def match_entries(entries, pattern):
  """A drop-in replacement for fnmatch.filter that supports pattern
  variants (ie. {foo,bar}baz = foobaz or barbaz)."""
  v1, v2 = pattern.find('{'), pattern.find('}')

  if v1 > -1 and v2 > v1:
    variations = pattern[v1+1:v2].split(',')
    variants = [ pattern[:v1] + v + pattern[v2+1:] for v in variations ]
    matching = []

    for variant in variants:
      matching.extend( fnmatch.filter(entries, variant) )

    return list( _deduplicate(matching) ) #remove dupes without changing order

  else:
    matching = fnmatch.filter(entries, pattern)
    matching.sort()
    return matching


def _deduplicate(entries):
  yielded = set()
  for entry in entries:
    if entry not in yielded:
      yielded.add(entry)
      yield entry

