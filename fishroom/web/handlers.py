#!/usr/bin/env python3
import functools
import json
import re
import tornado.escape
import tornado.web
import tornado.websocket
import tornado.gen as gen
import tornadoredis

import hashlib
from urllib.parse import urlparse, urljoin, urlencode
from datetime import datetime, timedelta
from .oauth import GitHubOAuth2Mixin
from ..db import get_redis as get_pyredis
from ..base import BaseBotInstance
from ..bus import MessageBus, MsgDirection
from ..helpers import get_now, tz
from ..models import Message, ChannelType, MessageType
from ..chatlogger import ChatLogger
from ..api_client import APIClientManager
from ..config import config


def get_redis():
    if config['redis'].get('unix_socket_path') is not None:
        r = tornadoredis.Client(
            unix_socket_path=config['redis']['unix_socket_path'])
    else:
        r = tornadoredis.Client(
            host=config['redis']['host'], port=config['redis']['port'])

    r.connect()
    return r

r = get_redis()
pr = get_pyredis()

mgb_im2fish = MessageBus(pr, MsgDirection.im2fish)


def authenticated(method):
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        if config.get('github', False) and not self.current_user:
            if self.request.method in ("GET", "HEAD"):
                url = self.get_login_url()
                self.redirect(url + "?" + urlencode(dict(next=self.request.uri)))
                return
            raise tornado.web.HTTPError(403)
        return method(self, *args, **kwargs)
    return wrapper


class BaseHandler(tornado.web.RequestHandler):
    def get_current_user(self):
        return self.get_secure_cookie("session")


class GitHubOAuth2LoginHandler(tornado.web.RequestHandler,
                               GitHubOAuth2Mixin):

    @gen.coroutine
    def get(self):
        if self.get_argument('code', False):
            logged_in = yield self.get_authenticated_user(code=self.get_argument('code'))
            if logged_in:
                self.set_secure_cookie('session', 'ok')
                self.redirect(self.get_argument('next', '/'))
            else:
                self.set_status(401)
                self.finish('Unauthorized')
        else:
            yield self.authorize_redirect(
                redirect_uri=config['baseurl'] + '/login?next=' + self.get_argument('next', '/'),
                client_id=config['github']['client_id'],
            )


class DefaultHandler(BaseHandler):

    @authenticated
    def get(self):
        url = "log/{room}/today".format(
            room=config["chatlog"]["default_channel"]
        )
        self.redirect(urljoin(config["baseurl"] + "/", url))


class RobotsTxtHandler(tornado.web.RequestHandler):

    def get(self):
        self.set_header('Content-Type', 'text/plain')
        self.write("User-agent: *\nDisallow: /")
        self.finish()


class TextStoreHandler(BaseHandler):

    @authenticated
    @gen.coroutine
    def get(self, room, date, msg_id):
        key = ChatLogger.LOG_QUEUE_TMPL.format(channel=room, date=date)
        msg_id = int(msg_id)
        val = pr.lrange(key, msg_id, msg_id)
        if not val:
            self.clear()
            self.set_status(404)
            self.finish("text not found")
            return
        msg = Message.loads(val[0].decode('utf-8'))
        # self.set_header('Content-Type', 'text/html')
        self.render(
            "text_store.html",
            title="Text from {}".format(msg.sender),
            content=msg.content,
            time="{date} {time}".format(date=msg.date, time=msg.time),
        )


class ChatLogHandler(BaseHandler):

    @authenticated
    @gen.coroutine
    def get(self, room, date):
        if room not in config["bindings"] or \
                room in config.get("private_rooms", []):
            self.set_status(404)
            self.finish("Room not found")
            return

        enable_ws = False
        if date == "today":
            enable_ws = True
            date = get_now().strftime("%Y-%m-%d")

        if ((get_now() - tz.localize(datetime.strptime(date, "%Y-%m-%d"))) >
                timedelta(days=7)):
            self.set_status(403)
            self.finish("Dark History Coverred")
            return

        embedded = self.get_argument("embedded", None)

        key = ChatLogger.LOG_QUEUE_TMPL.format(channel=room, date=date)
        mlen = pr.llen(key)

        last = int(self.get_argument("last", mlen)) - 1
        limit = int(self.get_argument("limit", 15 if embedded else mlen))

        start = max(last - limit + 1, 0)

        if self.get_argument("json", False):
            logs = pr.lrange(key, start, last)
            msgs = [json.loads(jmsg.decode("utf-8")) for jmsg in logs]
            for i, m in zip(range(start, last+1), msgs):
                m['id'] = i
                m.pop('opt', None)
                m.pop('receiver', None)
            self.set_header("Content-Type", "application/json")
            self.write(json.dumps(msgs))
            self.finish()
            return

        baseurl = config["baseurl"]
        p = urlparse(baseurl)

        dates = [(get_now() - timedelta(days=i)).strftime("%Y-%m-%d")
                 for i in range(7)]

        self.render(
            "chat_log.html",
            title="#{room} @ {date}".format(
                room=room, date=date),
            next_id=mlen,
            enable_ws=enable_ws,
            room=room,
            rooms=[
                x for x in config["bindings"].keys()
                if x not in config.get("private_rooms", ())
            ],
            date=date,
            dates=dates,
            basepath=p.path,
            embedded=(embedded is not None),
            limit=int(limit),
        )

    def name_style_num(self, text):
        m = hashlib.md5(text.encode('utf-8'))
        return "%d" % (int(m.hexdigest()[:8], 16) & 0x07)


class PostMessageHandler(BaseHandler):

    def set_default_headers(self):
        self.set_header("Content-Type", "application/json")

    def write_json(self, status_code, **kwargs):
        self.set_status(status_code)
        self.write(json.dumps(kwargs))

    @authenticated
    def post(self, room):
        if room not in config["bindings"] or \
                room in config.get("private_rooms", []):
            self.set_status(404)
            self.finish("Room not found")
            return

        if not config["bindings"].get(room, {}).get("web_post", True):
            message = "Web post is disabled."
            self.write_json(403, message=message)
            self.finish()
            return

        try:
            self.json_data = json.loads(self.request.body.decode('utf-8'))
        except ValueError:
            message = 'Unable to parse JSON.'
            self.write_json(400, message=message)  # Bad Request
            self.finish()
            return

        content = self.json_data.get("content", None)
        if not content:
            self.write_json(400, msg="Cannot send empty message")
            self.finish()
            return

        sender = str(self.json_data.get("nickname", '').strip())
        if not sender:
            self.write_json(400, msg="Nickname must be set")
            self.finish()
            return
        if not re.match(r'^\w', sender, flags=re.UNICODE):
            self.write_json(
                400, msg="Invalid char found, use a human's nickname instead!")
            self.finish()
            return

        now = get_now()
        date, time = now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S")
        mtype = MessageType.Command \
            if BaseBotInstance.is_cmd(content) \
            else MessageType.Text
        msg = Message(
            ChannelType.Web, sender, room, content=content,
            mtype=mtype, date=date, time=time, room=room
        )

        mgb_im2fish.publish(msg)
        self.write_json(200, msg="OK")
        self.finish()


class MessageStreamHandler(tornado.websocket.WebSocketHandler):

    def __init__(self, *args, **kwargs):
        super(MessageStreamHandler, self).__init__(*args, **kwargs)
        self.r = None

    def check_origin(self, origin):
        return True

    def on_message(self, jmsg):
        try:
            msg = json.loads(jmsg)
            self.r = get_redis()
            room = msg["room"]
            if room not in config["bindings"] or \
                    room in config.get("private_rooms", []):
                self.close()
                return
            self._listen(room)
        except:
            self.close()

    @gen.engine
    def _listen(self, room):
        self.redis_chan = ChatLogger.CHANNEL.format(channel=room)
        yield gen.Task(self.r.subscribe, self.redis_chan)
        self.r.listen(self._on_update)

    @gen.coroutine
    def _on_update(self, msg):
        if msg.kind == "message":
            self.write_message(msg.body)
        elif msg.kind == "subscribe":
            self.write_message("OK")
        elif msg.kind == "disconnect":
            self.close()

    def on_close(self):
        if self.r:
            if self.r.subscribed:
                self.r.unsubscribe(self.redis_chan)
            self.r.disconnect()


class APIRequestHandler(tornado.web.RequestHandler):

    mgr = APIClientManager(pr)

    def set_default_headers(self):
        self.set_header("Content-Type", "application/json")

    def write_json(self, status_code=200, **kwargs):
        self.set_status(status_code)
        self.write(json.dumps(kwargs))

    def auth(self):
        token_id = self.request.headers.get(
            "X-TOKEN-ID",
            self.get_argument("id", "")
        )
        token_key = self.request.headers.get(
            "X-TOKEN-KEY",
            self.get_argument("key", "")
        )
        fine = self.mgr.auth(token_id, token_key)
        if not fine:
            self.set_status(403)
            return
        return token_id


class APILongPollingHandler(APIRequestHandler):

    @gen.coroutine
    def get(self):
        token_id = self.auth()
        if token_id is None:
            self.finish("Invalid Token")
            return

        room = self.get_argument("room", None)
        if room not in config["bindings"] or \
                room in config.get("private_rooms", []):
            self.set_status(404)
            self.finish("Room not found")
            return

        queue = APIClientManager.queue_key.format(token_id=token_id)
        l = yield gen.Task(r.llen, queue)
        msgs = []
        if l > 0:
            msgs = yield gen.Task(r.lrange, queue, 0, -1)
            pr.delete(queue)
            msgs = [json.loads(m) for m in msgs]
        else:
            ret = yield gen.Task(r.blpop, queue, timeout=10)
            if queue in ret:
                msgs = [json.loads(ret[queue])]

        if room:
            msgs = [m for m in msgs if m['room'] == room]

        self.write_json(messages=msgs)
        self.finish()


class APIPostMessageHandler(APIRequestHandler):

    def prepare(self):
        if self.request.body:
            try:
                self.json_data = json.loads(self.request.body.decode('utf-8'))
            except ValueError:
                message = 'Unable to parse JSON.'
                self.write_json(400, message=message)  # Bad Request
                self.finish()
            return

        self.write_json(400, message="Cannot handle empty request")
        self.finish()

    def post(self, room):
        if room not in config["bindings"] or \
                room in config.get("private_rooms", []):
            self.set_status(404)
            self.finish("Room not found")
            return

        token_id = self.auth()
        if token_id is None:
            self.finish("Invalid Token")
            return

        content = self.json_data.get("content", None)
        if not content:
            self.write_json(400, message="Cannot send empty message")
            self.finish()

        apiname = self.mgr.get_name(token_id)
        sender = self.json_data.get("sender", apiname)
        now = get_now()
        date, time = now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S")
        chantag = "{}-{}".format(ChannelType.API, apiname)
        mtype = MessageType.Command \
            if BaseBotInstance.is_cmd(content) \
            else MessageType.Text
        msg = Message(
            chantag, sender, room, content=content,
            mtype=mtype, date=date, time=time, room=room
        )

        mgb_im2fish(msg)
        self.write_json(message="OK")
        self.finish()
