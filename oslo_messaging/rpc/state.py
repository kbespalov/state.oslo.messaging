import functools
import os
import socket
import time

import sys


class loop_bucket(object):
    """
       container for time loop metrics:
        min, max, sum, count
    """

    MIN = 0
    MAX = 1
    SUM = 2
    CNT = 3

    @classmethod
    def set(cls, bucket, value):
        bucket[cls.SUM] = value
        bucket[cls.CNT] = 1
        bucket[cls.MAX] = bucket[cls.MIN] = value

    @classmethod
    def add(cls, bucket, value):
        bucket[cls.SUM] += value
        bucket[cls.CNT] += 1
        bucket[cls.MIN] = min(bucket[cls.MIN], value)
        bucket[cls.MAX] = max(bucket[cls.MAX], value)

    @classmethod
    def create(cls):
        return [0, 0, 0, 0]

    @classmethod
    def get(cls, item, bucket):
        return bucket[item] if bucket else 0

    @classmethod
    def get_max(cls, bucket):
        return cls.get(cls.MAX, bucket)

    @classmethod
    def get_min(cls, bucket):
        return cls.get(cls.MIN, bucket)

    @classmethod
    def get_sum(cls, bucket):
        return cls.get(cls.SUM, bucket)

    @classmethod
    def get_cnt(cls, bucket):
        return cls.get(cls.CNT, bucket)

    @classmethod
    def get_avg(cls, bucket):
        return cls.get_sum(bucket) / (cls.get_cnt(bucket) or 1)


class TimeLoop(object):
    """
     The collection to persisting a time distribution values with
     specified time interval (time loop) and granularity.

     For example: if loop time is 60 min, granularity is 5 min.
     then self.buckets is list of 12 buckets:
     [0-5 min] [5-10 min] [10-15 min] ... [55-60 min]
     to each of them we are accumulate values by adding.
    """

    def __init__(self, loop_time, loop_granularity):
        self.loop_time = loop_time
        self.granularity = loop_granularity
        self.latest_action_time = 0
        self.latest_index = 0
        self.total_sum, self.total_calls = 0, 0
        self.global_min, self.global_max = 0, 0
        self.prev_loop_time = 0
        self.buckets_size = (loop_time / loop_granularity)
        self.buckets = []
        for _ in range(0, self.buckets_size):
            self.buckets.append(0)

    def get_index(self, time_value):
        return int((time_value % self.loop_time) / self.granularity)

    def add(self, value):

        self.global_max = max(value, self.global_max)
        self.global_min = min(value, self.global_min) or value

        cur_time = time.time()
        time_index = self.get_index(cur_time)
        bucket = self.buckets[time_index]
        # check if the bucket not initialized yet
        if not bucket:
            bucket = self.buckets[time_index] = loop_bucket.create()
        # cases then a loop cycle is done. needs the loop tail flushing.
        if time_index < self.latest_index or self.is_loop_expired(cur_time):
            self.flush(cur_time)
        # to set or accumulate value
        if time_index > self.latest_index:
            loop_bucket.set(bucket, value)
        else:
            loop_bucket.add(bucket, value)
        # flush the gap between consecutive insertions
        # [last_insertion][old_data][old_data][current_insertion]
        if time_index - self.latest_index > 1:
            for i in range(self.latest_index + 1, time_index):
                self.buckets[i] = 0
        self.total_sum += value
        self.total_calls += 1
        self.latest_index = time_index
        self.latest_action_time = cur_time

    def is_loop_expired(self, current_time):
        return current_time - self.latest_action_time >= self.loop_time

    def straighten_loop(self):
        straighten = []
        start = self.latest_index
        for i in xrange(self.buckets_size):
            start = (start + 1) % self.buckets_size
            straighten.append(self.buckets[start])
        return straighten

    def dump(self):
        return {'latest_call': self.latest_action_time,
                'runtime': {
                    'min': self.global_min,
                    'max': self.global_max,
                    'calls': self.total_calls,
                    'sum': self.total_sum
                },
                'distribution': self.straighten_loop()}

    def flush(self, ctime):
        self.prev_loop_time = self.latest_action_time
        flush_to = self.buckets_size - 1 if self.is_loop_expired(ctime) else self.get_index(ctime)
        for i in xrange(0, flush_to + 1):
            self.buckets[i] = 0


class RPCStateEndpoint(object):
    # namespace is used in order to avoid a methods name conflicts
    # target = Target(namespace="oslo.messaging.rpc_state")

    def __init__(self, server, target, loop_time=1800, granularity=10, profile=True):
        self.rpc_server = server
        self.target = target
        self.endpoints_state = {}
        self.start_time = time.time()
        self.loop_time = loop_time
        self.granularity = granularity
        self.start_time = time.time()
        self.worker_pid = os.getpid()
        self.process_name = os.path.basename(sys.argv[0])
        self.hostname = socket.gethostname()
        self.info = {'started': self.start_time,
                     'wid': self.worker_pid,
                     'hostname': self.hostname,
                     'proc_name': self.process_name,
                     'topic': self.target.topic,
                     'server': self.target.server,
                     'loop_time': self.loop_time,
                     'granularity': self.granularity}

        self._rpc_profile = profile
        self.processing_delay_loop = TimeLoop(loop_time, granularity)

    def get_endpoint_state(self, name):
        if name not in self.endpoints_state:
            self.endpoints_state[name] = {}
        return self.endpoints_state[name]

    def register_method(self, endpoint, method):
        if isinstance(endpoint, RPCStateEndpoint):
            return
        endpoint_state = self.get_endpoint_state(type(endpoint).__name__)
        if method not in endpoint_state:
            rpc_method = getattr(endpoint, method)
            wrapped = self.rpc_stats_aware(endpoint_state, rpc_method)
            setattr(endpoint, method, wrapped)

    def log_processing_delay(self, delay):
        self.processing_delay_loop.add(delay)

    def profiling_enabled(self):
        return self._rpc_profile

    def rpc_stats_aware(self, stats, method):

        method_name = method.__name__
        loop = TimeLoop(self.loop_time, self.granularity)
        stats[method_name] = loop

        @functools.wraps(method)
        def wrap(*args, **kwargs):
            if not self._rpc_profile:
                return method(*args, **kwargs)

            start = time.time()
            res = method(*args, **kwargs)
            end = time.time()
            duration = end - start
            loop.add(duration)
            return res

        return wrap

    def rpc_echo_reply(self, _, request_time):
        response = {'req_time': request_time}
        response.update(self.info)
        return response

    def dump_endpoints_stats(self, sample):
        endpoints_sample = sample['endpoints'] = {}
        for endpoint, methods in self.endpoints_state.iteritems():
            endpoints_sample[endpoint] = {}
            for method, loop in methods.iteritems():
                endpoints_sample[endpoint][method] = loop.dump()

    def runtime(self):
        return time.time() - self.start_time

    def disable_profiling(self, _):
        self._rpc_profile = False

    def enable_profiling(self, _):
        self._rpc_profile = True

    def rpc_stats(self, ctx, request_time):
        sample = dict(msg_type='sample',
                      req_time=request_time,
                      runtime=self.runtime(),
                      proc_delay=self.processing_delay_loop.dump())
        sample.update(self.info)
        self.dump_endpoints_stats(sample)
        return sample
