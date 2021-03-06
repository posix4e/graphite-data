# tests for an arbitrary DB
import os
import shutil
import time

from graphitedata.standard.whispertsdb import NewWhisperTSDB
from graphitedata.hbase.hbasedb import NewHbaseTSDB
import tsdb


def testCreateNodes(db):
    db.create("branch1.branch2.metric1",
              [(60, 24 * 60)],
              1,
              'sum',
              False,
              False)

    db.create("branch1.branch2.metric2",
              [(60, 24 * 60)],
              1,
              'sum',
              False,
              False)

    db.create("branch1.branch3.metric4",
              [(60, 24 * 60)],
              1,
              'sum',
              False,
              False)

    db.create("branch2.branch5.metric1",
              [(60, 24 * 60)],
              1,
              'sum',
              False,
              False)

    def assertFindsMetrics(pattern, metrics):
        foundMetrics = []
        for node in db.find_nodes(tsdb.FindQuery(pattern)):
            foundMetrics.append(node.path)
        for m in metrics:
            if m not in foundMetrics:
                raise AssertionError("Metric " + m + "not in nodes " + node.__str())


    assertFindsMetrics("*", ["branch1", "branch2"])
    assertFindsMetrics("*.*.*", ["branch1.branch2.metric1", "branch1.branch2.metric2", "branch1.branch3.metric4",
                                 "branch2.branch5.metric1"])
    assertFindsMetrics("*.branch2.*", ["branch1.branch2.metric1", "branch1.branch2.metric2"])
    assertFindsMetrics("*.branch{3,5}.*", ["branch1.branch3.metric4", "branch2.branch5.metric1"])
    print db.info("branch1.branch2.metric1")
    db.update_many("branch1.branch2.metric1", [(time.time(), 1.0), (time.time() - 120, 2.0)])
    #print db.get_intervals("branch1.branch2.metric1")
    node = db.find_nodes(tsdb.FindQuery("branch1.branch2.metric1")).next()
    print node.fetch(time.time() - 180, time.time())


def testData(db):
    db.create("data.metric1",
              [(60, 24 * 60), (3600, 24 * 7)], # retention is minutely for 1 day, then hourly for 1 week
              1,
              'sum',
              False,
              False)

    # log some minutely stats

    db.update_many("data.metric1", [(time.time(), 1.0), (time.time() - 120, 2.0)])

    node = db.find_nodes(tsdb.FindQuery("data.metric1")).next()

    print node.fetch(time.time() - 180, time.time())
    # now test data


print "testing whisper"
if os.path.exists("/tmp/whisper"):
    shutil.rmtree("/tmp/whisper", True)
if not os.path.exists("/tmp/whisper"):
    os.mkdir("/tmp/whisper")
db = NewWhisperTSDB("/tmp/whisper")
testCreateNodes(db)

print "testing hbase"
db = NewHbaseTSDB("localhost:9090:graphite_")
assert isinstance(db, tsdb.TSDB)

testCreateNodes(db)