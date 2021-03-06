#
# Copyright 2020 Laszlo Zeke
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

from flask import abort, Flask, request
import urllib
import json
import datetime
import logging
import re
from os import environ
from feedformatter import Feed
from cachetools import cached, TTLCache, LRUCache
from io import BytesIO
import gzip


VOD_URL_TEMPLATE = 'https://api.twitch.tv/kraken/channels/{login}/videos?broadcast_type=archive,highlight,upload&limit=10'
USERID_URL_TEMPLATE = 'https://api.twitch.tv/kraken/users?login={login}'
VODCACHE_LIFETIME = 10 * 60
USERIDCACHE_LIFETIME = 24 * 60 * 60
CHANNEL_FILTER = re.compile("^[a-zA-Z0-9_]{2,25}$")
TWITCH_CLIENT_ID = environ.get("TWITCH_CLIENT_ID")
PORT = environ.get("TWITCHRSS_PORT", "8080")
PORT = int(PORT)
DEBUG = True if environ.get('DEBUG') else False
logging.basicConfig(level=logging.DEBUG if DEBUG else logging.INFO)

if not TWITCH_CLIENT_ID:
    raise Exception("Twitch API client id is not set.")


app = Flask(__name__)


@app.route('/vod/<string:channel>', methods=['GET', 'HEAD'])
def vod(channel):
    channel = channel.lower()
    if CHANNEL_FILTER.match(channel):
        return get_inner(channel)
    else:
        abort(404)


@app.route('/vodonly/<string:channel>', methods=['GET', 'HEAD'])
def vodonly(channel):
    channel = channel.lower()
    if CHANNEL_FILTER.match(channel):
        return get_inner(channel, add_live=False)
    else:
        abort(404)


def get_inner(channel, add_live=True):
    userid_json = fetch_userid(channel)
    if not userid_json:
        abort(404)

    (channel_display_name, channel_id) = extract_userid(json.loads(userid_json))

    channel_json = fetch_vods(channel_id)
    if not channel_json:
        abort(404)

    decoded_json = json.loads(channel_json)
    rss_data = construct_rss(channel, decoded_json, channel_display_name, add_live)
    headers = {'Content-Type': 'application/rss+xml'}

    if 'gzip' in request.headers.get("Accept-Encoding", ''):
        headers['Content-Encoding'] = 'gzip'
        rss_data = gzip.compress(rss_data)

    return rss_data, headers


@cached(cache=TTLCache(maxsize=3000, ttl=USERIDCACHE_LIFETIME))
def fetch_userid(channel_name):
    return fetch_json(channel_name, USERID_URL_TEMPLATE)


@cached(cache=TTLCache(maxsize=500, ttl=VODCACHE_LIFETIME))
def fetch_vods(channel_id):
    return fetch_json(channel_id, VOD_URL_TEMPLATE)


def fetch_json(id, url_template):
    url = url_template.format(login=id)
    headers = {
        'Accept': 'application/vnd.twitchtv.v5+json',
        'Client-ID': TWITCH_CLIENT_ID,
        'Accept-Encoding': 'gzip',
    }
    request = urllib.request.Request(url, headers=headers)
    retries = 0
    while retries < 3:
        try:
            result = urllib.request.urlopen(request, timeout=3)
            logging.debug('Fetch from twitch for %s with code %s', id, result.getcode())
            if result.info().get('Content-Encoding') == 'gzip':
                logging.debug('Fetched gzip content')
                return gzip.decompress(result.read())
            return result.read()
        except Exception as e:
            logging.warning("Fetch exception caught: %s", e)
            retries += 1
    abort(503)


def extract_userid(user_info):
    userlist = user_info.get('users')
    if not userlist:
        logging.info('No such user found.')
        abort(404)
    # Get the first id in the list
    userid = userlist[0].get('_id')
    username = userlist[0].get('display_name')
    if username and userid:
        return username, userid
    else:
        logging.warning('Userid is not found in %s', user_info)
        abort(404)


def construct_rss(channel_name, vods_info, display_name, add_live=True):
    feed = Feed()

    # Set the feed/channel level properties
    feed.feed["title"] = f"{display_name}'s Twitch video RSS"
    feed.feed["link"] = "https://twitch.tv/"
    feed.feed["author"] = "Twitch RSS Generated"
    feed.feed["description"] = f"The RSS Feed of {display_name}'s videos on Twitch"
    feed.feed["ttl"] = '10'

    # Create an item
    try:
        for item in generate_items(vods_info, add_live=add_live, channel_name=channel_name):
            feed.items.append(item)
    except KeyError as e:
        logging.warning('Issue with json: %s\nException: %s', vods_info, e)
        abort(404)

    return feed.format_atom_string(pretty=True)


def generate_items(vods_info, *, channel_name, add_live):
    for vod in vods_info.get('videos', []):
        item = {}
        if vod["status"] == "recording":
            if not add_live:
                continue
            link = f"http://www.twitch.tv/{channel_name}"
            item["title"] = f"{vod['title']} - LIVE"
            item["category"] = "live"
        else:
            link = vod['url']
            item["title"] = vod['title']
            item["category"] = vod['broadcast_type']
        item["link"] = link
        img_src = vod['preview']['large']
        item["description"] = f'<a href="{link}"><img src="{img_src}" /></a>'
        if vod.get('game'):
            item["description"] += "<br/>" + vod['game']
        if vod.get('description_html'):
            item["description"] += "<br/>" + vod['description_html']
        
        d = datetime.datetime.strptime(vod['created_at'], '%Y-%m-%dT%H:%M:%SZ')
        item["pubDate"] = d.timetuple()
        
        item["guid"] = vod['_id']
        if vod["status"] == "recording":  # To show a different news item when recording is over
            item["guid"] += "_live"
        
        yield item

# For debug
if __name__ == "__main__":
    if DEBUG:
        app.run(host='127.0.0.1', port=PORT, debug=DEBUG)
