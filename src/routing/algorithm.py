import threading
import copy
import time
from .io import print_log

ALGORITHM_TYPE = "algorithm"


def log(message):
    print_log("[Algorithm] {0}".format(message))


def info(message):
    log("[INFO] {0}".format(message))


def error(message):
    log("[ERROR] {0}".format(message))


class Algorithm(object):
    def __init__(self, hostname, transport, routing_table, neighbor,
                 dispatcher, update_interval=30, timeout=180):
        self._hostname = hostname
        self._interval = update_interval
        self._timeout = timeout

        self._transport = transport
        self._routing = routing_table
        self._neighbor = neighbor

        self._timer_thread = None

        # dynamic update, structure
        #
        # self._routing_table = {
        #   'destination': {
        #     'next': next-hop hostname,
        #     'cost': integer
        #   },
        #   ...
        # }
        self._routing_table = {
            self._hostname: {
                'next': self._hostname,
                'cost': 0
            }
        }
        self._routing_table_lock = threading.Lock()

        # Link-State
        #
        # self._link_state = {
        #   "host_1": {
        #     "other_host1": cost_1_1,
        #     "other_host2": cost_1_2,
        #     ...
        #   },
        #   ...
        # }
        #
        self._link_state = {}
        self._link_state_lock = threading.Lock()

        # Alive-state
        #
        # self._alive_table = {
        #   "hostname": time,
        #   ...
        # }
        self._alive_table = {}
        self._alive_table_lock = threading.Lock()

        dispatcher.register(ALGORITHM_TYPE, self)
        self._neighbor.on_update(self._neighbor_update)

    # def inject(self, transport, routing_table, neighbor, dispather):
    #     self._transport = transport
    #     self._routing = routing_table
    #     self._neighbor = neighbor
    #     self._dispather = dispather

    def receive(self, src, data):
        pass

    def run(self):
        pass

    def stop(self):
        if self._timer_thread is not None:
            self._timer_thread.cancel()
            self._timer_thread = None

    def _push_to_routing_model(self):
        self._routing_table_lock.acquire()
        try:
            self._routing.update(self._routing_table)
        finally:
            self._routing_table_lock.release()

    def _neighbor_update(self, neighbor_table):
        log('new neighbor table: {}'.format(neighbor_table))
        self._routing_table_lock.acquire()
        try:
            for hostname in neighbor_table:
                if neighbor_table[hostname] != -1:
                    self._routing_table[hostname] = {
                        'next': hostname,
                        'cost': neighbor_table[hostname]
                    }
                    self._routing.update_one(hostname, hostname, neighbor_table[hostname])
        finally:
            self._routing_table_lock.release()

    def _neighbor_timeout(self, dead_hostnames):
        for hostname in dead_hostnames:
            self._neighbor.timeout(hostname)

class DV(Algorithm):
    def receive(self, src, data):
        modified = False
        dead_hostnames = []

        self._alive_table_lock.acquire()
        try:
            current_time = time.time()
            self._alive_table[self._hostname] = current_time
            self._alive_table[data['source']] = current_time
            dead_hostnames = [hostname
                              for hostname in self._alive_table
                              if current_time - self._alive_table[hostname] > self._timeout]
        finally:
            self._alive_table_lock.release()

        if len(dead_hostnames) != 0:
            log('{} dead hostnames: {}'.format(time.ctime(), dead_hostnames))
            self._neighbor_timeout(dead_hostnames)

        self._routing_table_lock.acquire()
        try:
            if len(set(self._routing_table.keys()) & set(dead_hostnames)) > 0:
                modified = True

            self._routing_table = {
                k: v for k, v in self._routing_table.items()
                if k not in dead_hostnames and v['next'] not in dead_hostnames
            }

            data_routing_table = {
                k: v for k, v in data['routing'].items()
                if k not in dead_hostnames and v['next'] not in dead_hostnames
            }

            neighbor_table = self._neighbor.get()
            for hostname in neighbor_table:
                if hostname not in dead_hostnames and hostname not in self._routing_table:
                    self._routing_table[hostname] = {
                        'next': hostname,
                        'cost': neighbor_table[hostname]
                    }

            for destination in data_routing_table:
                indirect_cost = self._routing_table[data['source']]['cost'] + \
                                data_routing_table[destination]['cost']
                if destination not in self._routing_table:
                    self._routing_table[destination] = {
                        'next': data['source'],
                        'cost': indirect_cost
                    }
                    modified = True
                elif self._routing_table[destination]['cost'] > indirect_cost:
                    self._routing_table[destination]['next'] = data['source']
                    self._routing_table[destination]['cost'] = indirect_cost
                    modified = True

            log('receive routing data from {}: {}'.format(data['source'],
                                                          data['routing']))
            log('routing table: {}'.format(self._routing_table))
        finally:
            self._routing_table_lock.release()

        if modified is True:
            self._notice_neighbor()

        self._push_to_routing_model()

    def run(self):
        self._notice_neighbor()

        self._timer_thread = threading.Timer(self._interval, DV.run, args=(self,))
        self._timer_thread.start()

    def _notice_neighbor(self):
        neighbor_table = self._neighbor.get()

        self._routing_table_lock.acquire()
        try:
            send_data = {
                'type': ALGORITHM_TYPE,
                'data': {
                    'source': self._hostname,
                    'routing': copy.deepcopy(self._routing_table)
                }
            }
        finally:
            self._routing_table_lock.release()

        for hostname in list(neighbor_table.keys()):
            self._transport.send(hostname, send_data)
            log('send routing data to {}: {}'.format(hostname, send_data['data']['routing']))


class LS(Algorithm):
    def receive(self, src, data):
        dead_hostnames = []
        neighbor_table = self._neighbor.get()

        self._alive_table_lock.acquire()
        try:
            current_time = time.time()
            self._alive_table[self._hostname] = current_time
            # update alive table
            for hostname in data['alive']:
                if hostname not in self._alive_table:
                    self._alive_table[hostname] = data['alive'][hostname]
                elif data['alive'][hostname] > self._alive_table[hostname]:
                    self._alive_table[hostname] = data['alive'][hostname]

            # collect dead hostnames
            for hostname in self._alive_table:
                if current_time - self._alive_table[hostname] > self._timeout:
                    dead_hostnames.append(hostname)
        finally:
            self._alive_table_lock.release()

        if len(dead_hostnames) != 0:
            log('{} dead hostnames: {}'.format(time.ctime(), dead_hostnames))
            self._neighbor_timeout(dead_hostnames)

        self._routing_table_lock.acquire()
        self._link_state_lock.acquire()
        try:
            self._link_state[self._hostname] = neighbor_table
            for hostname in neighbor_table:
                if hostname not in self._link_state:
                    self._link_state[hostname] = {}

            self._link_state[data['source']] = data['neighbor']
            for hostname in data['neighbor']:
                if hostname not in self._link_state:
                    self._link_state[hostname] = {}

            for hostname in dead_hostnames:
                if hostname in self._link_state:
                    self._link_state.pop(hostname)

            for hostname in self._link_state:
                self._link_state[hostname] = {
                    k: v for k, v in self._link_state[hostname].items()
                    if k not in dead_hostnames
                }

            prev_table = self._dijkstra()
            self._update_routing(prev_table)
            self._routing.update(copy.deepcopy(self._routing_table))

            log('receive routing data from {}: {}'.format(data['source'],
                                                          data['neighbor']))
            log('update routing table: {}'.format(self._routing_table))
        finally:
            self._link_state_lock.release()
            self._routing_table_lock.release()

        self._push_to_routing_model()

    def run(self):
        neighbor_table = self._neighbor.get()

        self._alive_table_lock.acquire()
        try:
            send_data = {
                'type': ALGORITHM_TYPE,
                'data': {
                    'source': self._hostname,
                    'neighbor': neighbor_table,
                    'alive': copy.deepcopy(self._alive_table)
                }
            }
        finally:
            self._alive_table_lock.release()

        self._transport.broadcasting(send_data)
        log('send neighbor information: {}'.format(send_data['data']['neighbor']))

        self._timer_thread = threading.Timer(self._interval, LS.run, args=(self,))
        self._timer_thread.start()

    def _dijkstra(self):
        """Dijkstra algorithm

        update routing table

        must be wrapped with the link state lock

        Returns:
            prev_table: shortest path table
        """

        visited = [self._hostname]
        prev_table = {
            self._hostname: {
                'prev': None,
                'cost': 0
            }
        }

        for hostname in self._link_state[self._hostname]:
            prev_table[hostname] = {
                'prev': self._hostname,
                'cost': self._link_state[self._hostname][hostname]
            }

        for hostname in self._link_state:
            if hostname not in prev_table:
                prev_table[hostname] = {
                    'prev': None,
                    'cost': -1
                }

        while True:
            nearest_hostname = None
            nearest_cost = -1

            # find w not in visited that D(w) is a minimum
            for hostname in prev_table:
                if hostname not in visited and \
                                prev_table[hostname]['cost'] != -1 and \
                        (nearest_cost == -1 or
                                 prev_table[hostname]['cost'] < nearest_cost):
                    nearest_hostname = hostname
                    nearest_cost = prev_table[hostname]['cost']

            if nearest_hostname is None:
                break

            visited.append(nearest_hostname)
            for hostname in self._link_state[nearest_hostname]:
                if hostname not in prev_table or \
                        (hostname not in visited and \
                                 (prev_table[hostname]['cost'] == -1 or
                                          prev_table[hostname]['cost'] > nearest_cost + \
                                              self._link_state[nearest_hostname][hostname])):
                    prev_table[hostname] = {
                        'prev': nearest_hostname,
                        'cost': nearest_cost + self._link_state[nearest_hostname][hostname]
                    }

        return prev_table

    def _update_routing(self, prev_table):
        """update routing table

        must be wrapped with the routing table lock

        Args:
            prev_table: calculated by _dijkstra
        """

        self._routing_table.clear()
        self._routing_table[self._hostname] = {
            'next': self._hostname,
            'cost': 0
        }

        for destination in prev_table:
            last_hop = destination

            if prev_table[last_hop]['prev'] is None:
                continue
            while prev_table[last_hop]['prev'] != self._hostname:
                last_hop = prev_table[last_hop]['prev']

            self._routing_table[destination] = {
                'next': last_hop,
                'cost': prev_table[destination]['cost']
            }


class CentralizedMember(LS):
    def __init__(self, central_hostname, hostname, transport, routing_table,
                 neighbor, dispather, update_interval=30, timeout=180):
        super(CentralizedMember, self).__init__(hostname,
                                                transport, routing_table, neighbor, dispather,
                                                update_interval, timeout)

        self._central_hostname = central_hostname

    def receive(self, src, data):
        current_time = time.time()

        neighbor_table = self._neighbor.get()

        # there is a time central_hostname not in neighbor table !!!
        central_cost = neighbor_table[self._central_hostname]

        self._routing_table_lock.acquire()
        self._link_state_lock.acquire()
        try:
            self._link_state = data['link']

            prev_table = self._dijkstra()
            self._update_routing(prev_table)
            self._routing_table[self._central_hostname] = {
                'next': self._central_hostname,
                'cost': central_cost
            }

            log('receive routing data from {}: {}'.format(data['source'], data['link']))
            log('update routing table: {}'.format(self._routing_table))
        finally:
            self._link_state_lock.release()
            self._routing_table_lock.release()

        self._push_to_routing_model()

    def run(self):
        send_data = {
            'type': ALGORITHM_TYPE,
            'data': {
                'source': self._hostname,
                'neighbor': self._neighbor.get()
            }
        }

        self._transport.send(self._central_hostname, send_data)
        log('send neighbor information to {}: {}'.format(self._central_hostname, send_data['data']['neighbor']))

        self._timer_thread = threading.Timer(self._interval, CentralizedMember.run, args=(self,))
        self._timer_thread.start()


class CentralizedController(Algorithm):
    def receive(self, src, data):
        dead_hostnames = []

        self._alive_table_lock.acquire()
        try:
            current_time = time.time()
            self._alive_table[data['source']] = current_time
            dead_hostnames = [hostname
                              for hostname in self._alive_table
                              if current_time - self._alive_table[hostname] > self._timeout]
        finally:
            self._alive_table_lock.release()

        if len(dead_hostnames) != 0:
            log('{} dead hostnames: {}'.format(time.ctime(), dead_hostnames))
            self._neighbor_timeout(dead_hostnames)

        dead_hostnames.append(self._hostname)
        self._link_state_lock.acquire()
        try:
            self._link_state[data['source']] = data['neighbor']
            for hostname in data['neighbor']:
                if hostname not in self._link_state:
                    self._link_state[hostname] = {}

            for hostname in dead_hostnames:
                if hostname in self._link_state:
                    self._link_state.pop(hostname)

            for hostname in self._link_state:
                self._link_state[hostname] = {
                    k: v for k, v in self._link_state[hostname].items()
                    if k not in dead_hostnames
                }

            log('receive routing data from {}: {}'.format(data['source'], data['neighbor']))
        finally:
            self._link_state_lock.release()

    def run(self):
        self._alive_table_lock.acquire()
        self._link_state_lock.acquire()
        try:
            current_time = time.time()
            send_data = {
                'type': ALGORITHM_TYPE,
                'data': {
                    'source': self._hostname,
                    'link': copy.deepcopy(self._link_state)
                }
            }
            alive_hosts = [hostname
                           for hostname in self._alive_table
                           if current_time - self._alive_table[hostname] <= self._timeout]
            dead_hosts = set(self._alive_table.keys()) - set(alive_hosts)
        finally:
            self._link_state_lock.release()
            self._alive_table_lock.release()

        self._neighbor_timeout(dead_hosts)

        for hostname in alive_hosts:
            self._transport.send(hostname, send_data)

        log('send routing data: {}'.format(send_data['data']['link']))

        self._timer_thread = threading.Timer(self._interval, CentralizedController.run, args=(self,))
        self._timer_thread.start()
