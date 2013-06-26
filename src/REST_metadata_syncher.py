
import boto
import datetime
import json
import logging
import Queue
import random
#import requests
import string
import sys
import threading
import time

import boto.s3.acl
import boto.s3.connection

from boto.connection import AWSAuthConnection
from operator import attrgetter
from RGW_REST_factory import RGW_REST_factory

#log_lock_time = 60 #seconds
#log_lock_time = 15 #seconds

#debug_commands = True
debug_commands = False
#deep_compare = True
deep_compare = False

class metadata_type:
    USER = 1
    BUCKET = 2

class REST_metadata_syncher:
    logging.basicConfig(filename='boto_metadata_syncher.log', level=logging.DEBUG)
    log = logging.getLogger(__name__)

    # guessing we'll need this later
    def __init__(self):
        pass

    # queries the number of metadata shards and then synches each shard, one at a time
    def sync_all_shards(self, source_access_key, source_secret_key, source_host, source_zone,
                       dest_access_key, dest_secret_key, dest_host, dest_zone,
                       num_worker_threads, log_lock_time):

        # construct the source connection object
        source_conn = boto.s3.connection.S3Connection(
            aws_access_key_id = source_access_key,
            aws_secret_access_key = source_secret_key,
            is_secure=False,
            host = source_host,
            calling_format = boto.s3.connection.OrdinaryCallingFormat(),
            debug=2
        )

        rest_factory = RGW_REST_factory()

        (ret, out) = rest_factory.rest_call(source_conn, 
                            ['log', 'list', 'type=metadata'], {"type":"metadata"})

        if 200 != ret:
            print 'log list type:metadata failed, code: ', ret
        elif debug_commands:
            print 'log list type:metadata returned code: ', ret
            print 'out: ', out()

        numObjects = out()['num_objects']
        print 'We have ', numObjects, ' master objects to check'

        # create a Queue of shards to sync
        workQueue = Queue.Queue()

        # list of threads that will sync the metadata shards
        threads = []
        threadID = 0
        # create the threads that will do the work
        for i in xrange(num_worker_threads):
            thread = REST_metadata_syncher_worker(threadID, workQueue, log_lock_time,
                                                  source_access_key, source_secret_key, 
                                                  source_host, source_zone,
                                                  dest_access_key, dest_secret_key,
                                                  dest_host, dest_zone)
            # make this thread run as a daemon thread
            thread.daemon = True
            thread.start()
            threads.append(thread)
            threadID += 1

        # now queue up the work to be done
        for i in xrange(numObjects):
            workQueue.put(i)

        # now wait for all the threads to finish
        workQueue.join()


# This class simply sleeps for N seconds, then sets the flag to relock the log file
#class log_lock_timer(threading.Thead):
#
#    time_to_sleep = None
#    bool_to_set = None
#
#    """timer class for relocking the given lock"""
#    def __init__(self, time_to_wait, flag):
#        threading.Thread.__init__(self)
#        self.time_to_sleep = time_to_wait
#        self.bool_to_set = flag


class REST_metadata_syncher_worker(threading.Thread):
    """Threaded metadata sync worker"""

    source_conn = None
    dest_conn = None
    source_zone = None
    dest_zone = None
    local_lock_id = None
    rest_factory = None
    threadID = None
    threadName = None
    queue = None
    relock_log = True
    relock_timer = None
    log_lock_time = None

    # sleep the prescribed amount of time and then set a bool to true.
    def flip_log_lock(self):
        self.relock_log = True

    def __init__(self, threadID, queue, log_lock_time,
                 source_access_key, source_secret_key, source_host, source_zone,
                 dest_access_key, dest_secret_key, dest_host, dest_zone):

        threading.Thread.__init__(self)
        self.source_zone = source_zone
        self.dest_zone = dest_zone
        self.threadID = threadID
        self.threadName = 'thread-' + str(self.threadID)
        self.queue = queue
        self.log_lock_time = log_lock_time

        # generates an N character random string from letters and digits 16 digits for now
        self.local_lock_id = \
          ''.join(random.choice(string.ascii_letters + string.digits) for x in range(16))
    
        # construct the two connection objects
        self.source_conn = boto.s3.connection.S3Connection(
            aws_access_key_id = source_access_key,
            aws_secret_access_key = source_secret_key,
            is_secure=False,
            host = source_host,
            calling_format = boto.s3.connection.OrdinaryCallingFormat(),
            debug=2
        )

        self.dest_conn = boto.s3.connection.S3Connection(
          aws_access_key_id = dest_access_key,
          aws_secret_access_key = dest_secret_key,
          is_secure=False,
          host = dest_host,
          calling_format = boto.s3.connection.OrdinaryCallingFormat(),
        )

        self.rest_factory = RGW_REST_factory()


    # we explicitly specify the connection to use for the locking here  
    # in case we need to lock a non-master log file
    def acquire_log_lock(self, conn, lock_id, zone_id, shard_num):
      
        print shard_num, ' lock (re)acquired'
        (retVal, out) = self.rest_factory.rest_call(conn, ['log', 'lock'], 
            {"type":"metadata", "id":shard_num, "length":self.log_lock_time, 
             'zone-id': zone_id, "locker-id":lock_id})

        if 200 != retVal:
            print 'acquire_log_lock for zone ', zone_id, ' failed, returned http code: ', retVal
            return retVal

        elif debug_commands:
            print 'acquire_log_lock for zone ', zone_id, ' returned: ', retVal

        self.relock_log = False

        # start the timer to relock this log 
        self.relock_timer = threading.Timer(0.85 * self.log_lock_time, self.flip_log_lock) 
        self.relock_timer.start()

        return retVal


    # we explicitly specify the connection to use for the locking here  
    # in case we need to lock a non-master log file
    def release_log_lock(self, conn, lock_id, zone_id, shard_num):
        (retVal, out) = self.rest_factory.rest_call(self.source_conn, ['log', 'unlock'], {
                                   'type':'metadata', 'id':shard_num, 
                                   'locker-id':lock_id, 'zone-id':zone_id})

        if 200 != retVal:
            print 'metadata log unlock for zone ', zone_id, ' failed, returned http code: ', retVal
        elif debug_commands:
            print 'metadata log unlock for zone ', zone_id, ' returned: ', retVal

        return retVal

    # we've already done all the logical checks, just delete it at this point
    def delete_remote_entry(self, conn, entry_name, md_type):
        # get current info for this entry 
        if md_type == metadata_type.USER:
            (retVal, src_acct) = self.rest_factory.rest_call(conn, 
                                ['user', 'delete', entry_name], {}, admin=True)
        elif md_type == metadata_type.BUCKET:
            (retVal, src_acct) = self.rest_factory.rest_call(conn, 
                                ['bucket', 'delete', entry_name], {}, admin=False)
        else:
            # invalid metadata type, return an http error code
            print 'invalid metadata_type found in delete_remote_entry(). value: ', md_type
            return 404

        if 200 != retVal:
            print 'delete entry failed for ', entry_name, '; returned http code: ', retVal
        elif debug_commands:
            print 'delete entry for ', entry_name, '; returned http code: ', retVal

        return retVal

    # copies the curret metadata for a user from the master side to the 
    # non-master side
    def add_entry_to_remote(self, entry_name, md_type):

        # get current info for this entry 
        if md_type == metadata_type.USER:
            (retVal, src_acct) = self.rest_factory.rest_call(self.source_conn, 
                                ['metadata', 'metaget', 'user'], {'key':entry_name})
        elif md_type == metadata_type.BUCKET:
            (retVal, src_acct) = self.rest_factory.rest_call(self.source_conn, 
                                ['metadata', 'metaget', 'bucket'], {'key':entry_name})
        else:
            # invalid metadata type, return an http error code
            print 'invalid metadata_type found in add_entry_to_remote(). value: ', md_type
            return 404

        if 200 != retVal:
            print 'add_entry_to_remote source side metadata (GET) failed, code: ', retVal
            return retVal
        elif debug_commands:
            print 'add_entry_to_remote metadata get() returned: ', retVal

        # create an empty dict and pull out the name to use as an argument for next call
        args = {}
        if md_type == metadata_type.USER:
            args['key'] = src_acct()['data']['user_id']
        elif md_type == metadata_type.BUCKET:
            args['key'] = src_acct()['data']['bucket_info']['bucket']['name']
        else:
            # invalid metadata type, return an http error code
            print 'invalid metadata_type found in add_entry_to_remote(). value: ', md_type
            return 404

        # json encode the data
        outData = json.dumps(src_acct())

        if md_type == metadata_type.USER:
            (retVal, dest_acct) = self.rest_factory.rest_call(self.dest_conn, 
                                ['metadata', 'metaput', 'user'], args, data=outData)
        elif md_type == metadata_type.BUCKET:
            (retVal, dest_acct) = self.rest_factory.rest_call(self.dest_conn, 
                                ['metadata', 'metaput', 'bucket'], args, data=outData)
        else:
            # invalid metadata type, return an http error code
            print 'invalid metadata_type found in add_entry_to_remote(). value: ', md_type
            return 404


        if 200 != retVal:
            print 'metadata user (PUT) failed, return http code: ', retVal
            print 'body: ', dest_acct()
            return retVal
        elif debug_commands:
            print 'add_entry_to_remote metadata userput() returned: ', retVal


        return retVal

     # for now, just reuse the add_entry code. May need to differentiate in the future
    def update_remote_entry(self, entry, md_type):
        return self.add_entry_to_remote(entry, md_type)


    # use the specified connection as it may be pulling metadata from any rgw
    def pull_metadata_for_entry(self, conn, entry_name, md_type):

        if md_type == metadata_type.USER:
            (retVal, out) = self.rest_factory.rest_call(conn, ['metadata', 'metaget', 'user'], {"key":entry_name})
        elif md_type == metadata_type.BUCKET:
            (retVal, out) = self.rest_factory.rest_call(conn, ['metadata', 'metaget', 'bucket'], {"key":entry_name})
        else:
            # invalid metadata type, return an http error code
            print 'invalid metadata_type found in pull_metadata_for_entry(). value: ', md_type
            return 404

        if 200 != retVal and 404 != retVal:
            print 'pull_metadata_for_entry() metadata user(GET) failed for {entry} returned {val}'.format(entry=entry_name,val=retVal)
            return retVal, None
        else:
            if debug_commands:
                print 'pull_metadata_for_entry for {entry} returned: {val}'.format(entry=entry_name,val=retVal)

        return retVal, out()

    # only used for debugging at present
    def manually_diff(self, source_data, dest_data):
        diffkeys1 = [k for k in source_data if source_data[k] != dest_data[k]]
        diffkeys2 = [k for k in dest_data if dest_data[k] != source_data[k] and not (k in source_data) ]
    
        return diffkeys1 + diffkeys2

    def manually_diff_individual_user(self, uid):
        retVal = 200 # default to success
   
        # get user metadata from the source side
        (retVal, source_data) = self.pull_metadata_for_uid(self.source_conn, uid)
        if 200 != retVal:
            return retVal

        # get the metadata for the same uid from the dest side
        (retVal, dest_data) = self.pull_metadata_for_uid(self.dest_conn, uid)
        if 200 != retVal:
            return retVal

        diff_set = self.manually_diff(source_data, dest_data)
   
        if 0 == len(diff_set):
            print 'deep comparison of uid ', uid, ' passed'
        else:
            for k in diff_set:
                print k, ':', source_data[k], ' != ', dest_data[k]
                retVal = 400 # throw an http-ish error code
   
        return retVal

    # this is used to delete either a user or a bucket create/destroy, since
    # both use the same APIs
    def delete_meta_entry(self, conn, entry_name, md_type, tag=None):
        retVal = 200

        # this should be a 404, as the entry was deleted from the source 
        # (which is why it's in the log as a delete) or a 200 (if it was
        # deleted and then the same entry was added in the same logging window)
        retVal, source_data = self.pull_metadata_for_entry(self.source_conn, entry_name, md_type)
        if 200 != retVal and 404 != retVal:
            print 'pull from source failed for entry ', entry_name, '; ', retVal, ' was returned'
            return retVal

        retVal, dest_data = self.pull_metadata_for_entry(self.dest_conn, entry_name, md_type)
        if 200 != retVal:
            # if the entry does not exist on the non-master zone for some reason, then 
            # our work is done here, so return a success on a 404
            if 404 == retVal:
                return 200
            else:
                print 'pull from dest failed for entry ', entry_name, '; ', \
                      retVal, ' was returned'
                return retVal

        # If the tag in the metadata log on the dest_data does not match the current tag
        # for the entry, skip this entry.
        # We assume this is caused by this entry being for a entry that has been deleted.
        if tag != None and tag != dest_data['ver']['tag']:
            print 'log tag ', tag, ' != current entry tag ', dest_data['ver']['tag'], \
                  '. Skipping this entry / tag pair (', entry_name, " / ", tag, ")"
            #return retVal
            return 200

        retVal = self.delete_remote_entry(conn, entry_name, md_type)
        if 200 != retVal:
            print 'delete_remote_entry() failed for entry ', entry_name, '; ', \
                      retVal, ' was returned'
            return retVal
        elif debug_commands:
            print 'delete_remote_entry() for entry ', entry_name, '; ', \
                      ' returned ', retVal

        return retVal

    # this is used to check either a user or a bucket create/destroy, since
    # both use the same APIs
    def check_meta_entry(self, entry_name, md_type, tag=None):
        retVal = 200
        print 'in check_meta_entry()'

        retVal, source_data = self.pull_metadata_for_entry(self.source_conn, entry_name, md_type)
        if 200 != retVal:
            print 'pull from source failed for entry ', entry_name
            return retVal
        elif debug_commands:
            print 'in check_meta_entry(), pull_metadata_for_entry() returned ', retVal, \
                  ' for entry: ', entry_name

        # If the tag in the metadata log on the source_data does not match the current tag
        # for the entry, skip this entry.
        # We assume this is caused by this entry being for a entry that has been deleted.
        if tag != None and tag != source_data['ver']['tag']:
            print 'log tag ', tag, ' != current entry tag ', source_data['ver']['tag'], \
                  '. Skipping this entry / tag pair (', entry_name, " / ", tag, ")"
            #return retVal
            return 200

        # if the user does not exist on the non-master side, then a 404 
        # return value is appropriate
        retVal, dest_data = self.pull_metadata_for_entry(self.dest_conn, entry_name, md_type)
        if 200 != retVal and 404 != retVal:
            print 'pull from dest failed for entry: ', entry_name
            return retVal

        # if this user does not exist on the destination side, add it
        if (retVal == 404 and dest_data['Code'] == 'NoSuchKey'):
            print 'entry: ', entry_name, ' missing from the remote side. Adding it'
            retVal = self.add_entry_to_remote(entry_name, md_type)

        else: # if the user exists on the remote side, ensure they're the same version
            dest_ver = dest_data['ver']['ver']
            source_ver = source_data['ver']['ver']

            if dest_ver != source_ver:
                print 'entry: ', entry_name, ' local_ver: ', source_ver, ' != dest_ver: ', dest_ver, ' UPDATING'
                retVal = self.update_remote_entry(entry_name, md_type)
            elif debug_commands:
                print 'entry: ', entry_name, ' local_ver: ', source_ver, ' == dest_ver: ', dest_ver 

        return retVal


    # metadata changes are grouped into shards based on [ uid | tag ] TODO, figure this out
    #def process_shard(self, shard_num):
    def run(self):
        while True:
            shard_num = self.queue.get()
            print shard_num, ' is being processed by thread ', self.threadName
    
            # we need this due to a bug in rgw that isn't auto-filling in sensible defaults
            # when start-time is omitted
            really_old_time = "2010-10-10 12:12:00"
    
            # NOTE rgw deals in UTC time. Make sure you adjust your calls accordingly
            sync_start_time = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    
            # first, lock the log
            self.acquire_log_lock(self.source_conn, self.local_lock_id, \
                                  self.source_zone, shard_num)
    
            (ret, out) = self.rest_factory.rest_call(self.source_conn, 
                                   ['log', 'list', 'id=' + str(shard_num)], 
                                   {"type":"metadata", "id":shard_num})
    
            if 200 != ret:
                print 'metadata list failed, returned http code: ', ret
            elif debug_commands:
                print 'metadata list returned: ', ret
    
            log_entry_list = out()
            metadata_entries = {}
    
            print 'shard ', shard_num, ' has ', len(log_entry_list), ' entries'
            # sort the data by reverse status (so write comes prior to completed)
            mySorted = \
                sorted(log_entry_list, key=lambda entry: entry['data']['status']['status'], 
                       reverse=True)
            # then sort by read_version
            mySorted2 = \
                sorted(mySorted, key=lambda entry: int(entry['data']['read_version']['ver']))      
  
            # finally, by name. Not that Python's sort is stable, so the end result is the 
            # entries sorted by name, then by version and finally by status.
            mySorted3 = sorted(mySorted2, key=lambda entry: entry['name'])        
    
            # iterate over the sorted keys to find the highest logged version for each
            # uid / tag combo (unique instance of a given uid
            for entry in mySorted3:
                # use both the uid and tag as the key since a uid may have 
                # been deleted and re-created
                section = entry['section'] # should be just 'user' or 'bucket'
                name = entry['name']
                tag = entry['data']['write_version']['tag']
                ver = entry['data']['write_version']['ver']
                status = entry['data']['status']['status']
                compKey = name + "@" + tag
    
                # test if there is already an entry in the dictionary for the user
                if (metadata_entries.has_key(compKey)): 
                    # if there is, then only add this one if the ver is higher
                    if (metadata_entries[compKey] < ver):
                        metadata_entries[compKey] = section, ver, status
                else: # if not, just add this entry
                    metadata_entries[compKey] = section, ver, status
    
            # sync each entry / tag pair
            # bail on any user where a non-200 status is returned
            for key,value in metadata_entries.iteritems():
                #time.sleep(21) # just for testing, to trigger log re-locking
                if self.relock_log:
                    self.acquire_log_lock(self.source_conn, self.local_lock_id, \
                                          self.source_zone, shard_num)

                name, tag = key.split('@')
                section, ver, status = value
                if status == 'remove':
                    if section == 'user':
                        print 'removing user ', name
                        retVal = self.delete_meta_entry(self.dest_conn, name, \
                                                        metadata_type.USER, tag=tag)
                    elif section == 'bucket':
                        print 'removing bucket', name
                        retVal = self.delete_meta_entry(self.dest_conn, name, \
                                                        metadata_type.BUCKET, tag=tag)
                    else:
                        print 'found unknown metadata type: ', section, '. bailing'
                        retVal = 500

                elif status == 'write':
                    if section == 'user':
                        print 'writing user ', name
                        retVal = self.check_meta_entry(name, metadata_type.USER, tag=tag)
                    elif section == 'bucket':
                        print 'writing bucket ', name
                        retVal = self.check_meta_entry(name, metadata_type.BUCKET, tag=tag)
                    else:
                        print 'found unknown metadata type: ', section, '. bailing'
                        retVal = 500
                else:
                    print 'doing something???? to ', name, ' section: ', status
                    retVal = 500
    
    
                if 200 != retVal:
                    print 'check_meta_entry() returned http code ', retVal, ' for', name
                    # we hit an error processing a user. Bail and unlock the log
                    self.release_log_lock(self.source_conn, self.local_lock_id, \
                                          self.source_zone, shard_num)
                    return retVal

                elif debug_commands:
                    print 'check_meta_entry() returned http code ', retVal, ' for', name
  
            # trim the log for this shard now that all the users are synched
            # this should only occur if no users threw errors
            (retVal, out) = self.rest_factory.rest_call(self.source_conn, ['log', 'trim', 'id=' + str(shard_num)], {"id":shard_num, "type":"metadata", "start-time":really_old_time, "end-time":sync_start_time})
  
            if 200 != retVal:
                print 'log trim returned http code ', retVal
                # we hit an error processing a user. Bail and unlock the log
                self.release_log_lock(self.source_conn, self.local_lock_id, \
                                      self.source_zone, shard_num)
                return retVal
            elif debug_commands:
                print 'log trim returned http code ', retVal
  
            # finally, unlock the log
            self.release_log_lock(self.source_conn, self.local_lock_id, \
                                  self.source_zone, shard_num)
  
            #return ret
            print shard_num, ' is done being processed by thread ', self.threadName
  
            # let the queue know that this shard has been processed
            self.queue.task_done()


