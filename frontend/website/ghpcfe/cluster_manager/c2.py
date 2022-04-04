# Copyright 2022 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from google.cloud import pubsub
from google.api_core.exceptions import AlreadyExists
import json, uuid

# Note: We can't import Models here, because this module gets run as part of
# startup, and the Models haven't yet been created.
# Instead, import Models in the Callback Functions as appropriate

from . import utils

import logging
logger = logging.getLogger(__name__)


# Current design:
#  1 topic for our overall system
#  Subscription for FE is set with a filter of messages WITHOUT a "target"
#  attribute.  Subscriptions for Clusters each have a filter for a "target"
#  attribute that matches the cluster's ID.  (ideally, should rather be
#  something less guessable, like a hash or a unique key.)




# Message data should be json-encoded.
# Message attributes are to be used by the C2-infrastructure - data for the wider use
# Messages should have the following attributes:
#   * target={subscription_name}  (or no target, to come to FE)
#   * command=('ping', 'sub_job', etc...)
# If no 'target' (aka, coming from the clusters:
#   * source={cluster_id} - Who sent it?


# Command with reponse callback
#
# Commands that require a response should encode a unique key as a message
# field ('ackid').
# When receiver finishes the command, they should then send an ACK with that
# same 'ackid', and any associated data.




_c2_callbackMap = {}


def c2_ping(message, source_id):
    # Expect source_id in the form of 'cluster_{id}'
    logger.info(f"Received PING from cluster {source_id}")
    if 'id' in message:
        logger.info(f"    PING id {id}. Sending ACK")
        pid = message['id']
        _c2State.send_message('PONG', {'id': pid}, target=source_id)
    return True

def c2_pong(message, source_id):
    # Expect source_id in the form of 'cluster_{id}'
    logger.info(f"Received PONG from cluster {source_id}")
    if 'id' in message:
        logger.info(f"    PING id {id}.")
    return True



_c2_ackMap = {}
# Difference between UPDATE and ACK:  ACK removes the callback, UPDATE leaves it in place
def cb_ack(message, source_id):
    ackid = message.get('ackid', None)
    logger.info(f"Received ACK to message {ackid} from {source_id}!")
    if not ackid:
        logger.error("No ackid in ACK.  Ignoring")
        return True
    try:
        cb = _c2_ackMap.pop(ackid)
        logger.info("Calling Callback registered for this ACK")
        cb(message)
    except KeyError:
        logger.warning("No Callback registered for the ACK")
        pass

    return True

# Difference between UPDATE and ACK:  ACK removes the callback, UPDATE leaves it in place
def cb_update(message, source_id):
    ackid = message.get('ackid', None)
    logger.info(f"Received UPDATE to message {ackid} from {source_id}!")
    if not ackid:
        logger.error("No ackid in UPDATE.  Ignoring")
        return True
    try:
        cb = _c2_ackMap[ackid]
        logger.info("Calling Callback registered for this UPDATE")
        cb(message)
    except KeyError:
        logger.warning("No Callback registered for the UPDATE")
        pass

    return True


def cb_cluster_status(message, source_id):
    from ..models import Cluster
    try:
        cid = message['cluster_id']
        if f"cluster_{cid}" != source_id:
            raise ValueError("Message comes from {source_id}, but claims cluster {cid}.  Ignoring message")

        cluster = Cluster.objects.get(pk=cid)
        logger.info(f"Cluster Status message for {cluster.id}: {message['message']}")
        new_status = message.get('status', None)
        if new_status:
            cluster.status = new_status
            cluster.save()
    except Exception as ex:
        logger.error(f"Cluster Status Callback error!", exc_info=ex)
    return True
    



def _c2_respCB(message):
    logger.debug(f"Received {message}.")

    cmd = message.attributes.get('command', None)
    try:
        source = message.attributes.get('source', None)
        if not source:
            logger.error("Message had no Source ID")

        callback = _c2_callbackMap[cmd]
        if callback(json.loads(message.data), source_id=source):
            message.ack()
        else:
            message.nack()
        return
    except KeyError:
        if cmd:
            logger.error(f"Message command {cmd} associated with it, but No such command known.  Discarding")
        else:
            logger.error("Message has no command associated with it. Discarding")
    message.ack()





class _C2State:
    def __init__(self):
        self._pubClient = None
        self._subClient = None
        self._streaming_pull_future = None
        self._project_id = None
        self._topic = None
        self._topic_path = None


    @property
    def subClient(self):
        if not self._subClient:
            self._subClient = pubsub.SubscriberClient()
        return self._subClient

    @property
    def pubClient(self):
        if not self._pubClient:
            self._pubClient = pubsub.PublisherClient()
        return self._pubClient


    def startup(self):
        conf = utils.load_config()
        self._project_id = conf['server']['gcp_project']
        self._topic = conf['server']['c2_topic']
        self._topic_path = self.pubClient.topic_path(self._project_id, self._topic)

        sub_path = self.get_or_create_subscription("c2resp", filter_target=False)

        self._streaming_pull_future = self.subClient.subscribe(sub_path, callback=_c2_respCB)
        # TODO... Figure out hot to "cleanly" shut down


    def get_subscription_path(self, sub_id):
        sub_id = f"{self._topic}-{sub_id}"
        return self.subClient.subscription_path(self._project_id, sub_id)

    def get_or_create_subscription(self, sub_id, filter_target=True, service_account=None):
        sub_path = self.get_subscription_path(sub_id)

        request = {'name': sub_path,
                'topic': self._topic_path
        }
        if filter_target:
            request['filter'] = f'attributes.target="{sub_id}"'
        else:
            request['filter'] = f'NOT attributes:target'

        try:
            # Create subscription if it doesn't already exist
            self.subClient.create_subscription(request=request)
            logger.info(f"PubSub Subscription created")

            if service_account:
                self.setup_service_account(sub_id, service_account)
            
        except AlreadyExists:
            logger.info(f"PubSub Subscription {sub_path} already exists")
            pass
        logger.info(f"Returning subscription {sub_path}")
        return sub_path


    def setup_service_account(self, sub_id, service_account):
        sub_path = self.get_subscription_path(sub_id)
        # Need to set 2 policies.  One on the subscription, to allow
        # access to subscribe one on the main topic, to allow
        # publication (c2 response)

        policy = self.subClient.get_iam_policy(request={"resource": sub_path})
        policy.bindings.add(role='roles/pubsub.subscriber', members=[f"serviceAccount:{service_account}"])
        policy = self.subClient.set_iam_policy(request={"resource": sub_path, "policy": policy})

        policy = self.pubClient.get_iam_policy(request={"resource": self._topic_path})
        policy.bindings.add(role='roles/pubsub.publisher', members=[f"serviceAccount:{service_account}"])
        policy = self.pubClient.set_iam_policy(request={"resource": self._topic_path, "policy": policy})


    def delete_subscription(self, sub_id, service_account):
        sub_path = self.get_subscription_path(sub_id)
        self.subClient.delete_subscription(request={"subscription": sub_path})
        if service_account:
            # TODO:  Remove IAM permission from topic
            #policy = self.pubClient.get_iam_policy(request={"resource": sub_path})
            #policy.bindings.remove(role='roles/pubsub.publisher', members=[f"serviceAccount:{service_account}"])
            #policy = self.pubClient.set_iam_policy(request={"resource": sub_path, "policy": policy})
            pass


    def send_message(self, command, message, target, extra_attrs={}):
        # TODO: If we want loopback, need to make 'target' optional,
        # or change up our filters
        # TODO: Consider if we want to keep the futures or not
        self.pubClient.publish(self._topic_path, bytes(json.dumps(message), 'utf-8'), target=target, command=command, **extra_attrs)


_c2State = None




def get_cluster_sub_id(cluster_id):
    return f"cluster_{cluster_id}"

def get_cluster_subscription_path(cluster_id):
    return _c2State.get_subscription_path(get_cluster_sub_id(cluster_id))


def create_cluster_subscription(cluster_id):
    return _c2State.get_or_create_subscription(get_cluster_sub_id(cluster_id), filter_target=True)


def add_cluster_subscription_service_account(cluster_id, service_account):
    return _c2State.setup_service_account(get_cluster_sub_id(cluster_id), service_account)


def delete_cluster_subscription(cluster_id, service_account=None):
    return _c2State.delete_subscription(get_cluster_sub_id(cluster_id), service_account=service_account)

def get_topic_path():
    return _c2State._topic_path



def startup():
    global _c2State
    if _c2State:
        logger.error("ERROR:  C&C PubSub already started!")
        return

    _c2State = _C2State()
    _c2State.startup()
# Difference between UPDATE and ACK:  ACK removes the callback, UPDATE leaves it in place
    register_command('ACK', cb_ack)
    register_command('UPDATE', cb_update)
    register_command('PING', c2_ping)
    register_command('PONG', c2_pong)
    register_command('CLUSTER_STATUS', cb_cluster_status)


def send_command(cluster_id, cmd, data, onResponse=None):
    if onResponse:
        data['ackid'] = str(uuid.uuid4())
        _c2_ackMap[data['ackid']] = onResponse
    _c2State.send_message(command=cmd, message=data, target=get_cluster_sub_id(cluster_id))
    return data['ackid']

def send_update(cluster_id, comm_id, data):
    # comm_id is result from `send_command()`
    data['ackid'] = comm_id
    _c2State.send_message(command='UPDATE', message=data, target=get_cluster_sub_id(cluster_id))


def register_command(command_id, callback):
    _c2_callbackMap[command_id] = callback


