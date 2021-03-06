# -*- coding: utf-8 -*-

import re
import threading
import traceback

from repoze.lru import lru_cache

import youtube_dl

import json
from itertools import islice
import pykka

import requests

from mopidy_youtube import logger
from mopidy import httpclient

# Making HTTP requests from extensions
# https://docs.mopidy.com/en/latest/extensiondev/#making-http-requests-from-extensions

def get_requests_session(proxy_config, user_agent):
    proxy = httpclient.format_proxy(proxy_config)
    full_user_agent = httpclient.format_user_agent(user_agent)

    session = requests.Session()
    session.proxies.update({'http': proxy, 'https': proxy})
    session.headers.update({'user-agent': full_user_agent})
    
    return session

# decorator for creating async properties using pykka.ThreadingFuture
# A property 'foo' should have a future '_foo'
# On first call we invoke func() which should create the future
# On subsequent calls we just return the future
#
def async_property(func):
    _future_name = '_' + func.__name__

    def wrapper(self):
        if _future_name not in self.__dict__:
            apply(func, (self,))   # should create the future
        return self.__dict__[_future_name]

    return property(wrapper)


# The Video / Playlist classes can be used to load YouTube data. If
# 'api_enabled' is true (and a valid api_key supplied), most data are loaded
# using the (very much faster) YouTube Data API. If 'api_enabled' is false, most
# data are loaded using requests and regex. Using requests and regex is many
# times slower than using the API.

# overridable by config
api_enabled = False

## Maybe we should keep the APIs separate, and only import the one that will be used?
## And then just call 'API'?
#
# if api_enabled:
#     from youtube_API import API as API
# else
#     from youtube_scrAPI as scrAPI as API
#
# In both cases audio_url is loaded via youtube_dl (slower). All properties
# return futures, which gives the possibility to load info in the background
# (using threads), and use it later.
#
# eg
#   video = youtube.Video.get('7uj0hOIm2kY')
#   video.length   # non-blocking, returns future
#   ... later ...
#   print video.length.get()  # blocks until info arrives, if it hasn't already
#
# Entry is a base class of Video and Playlist
#
class Entry(object):
    cache_max_len = 400

    # Use Video.get(id), Playlist.get(id), instead of Video(id), Playlist(id),
    # to fetch a cached object, if available
    #
    @classmethod
    @lru_cache(maxsize=cache_max_len)
    def get(cls, id):
        obj = cls()
        obj.id = id
        return obj

    # Search for both videos and playlists using a single API call. Fetches
    # only title, thumbnails, channel (extra queries are needed for length and
    # video_count)
    #
    @classmethod
    def search(cls, q):
        def create_object(item):
            if item['id']['kind'] == 'youtube#video':
                obj = Video.get(item['id']['videoId'])
                ## check if ['contentDetails'] exists in item and if so, ask for length
                # if 'contentDetails' in item:
                #     obj._set_api_data(['title', 'channel', 'length'], item)
                # else:
                #     obj._set_api_data(['title', 'channel'], item)
                obj._set_api_data(['title', 'channel'], item)
            else:
                obj = Playlist.get(item['id']['playlistId'])
                ## check if ['contentDetails'] exists in item and if so, ask for video_count
                # if 'contentDetails' in item:
                #     obj._set_api_data(['title', 'channel', 'thumbnails', 'video_count'], item)
                # else:
                #     obj._set_api_data(['title', 'channel', 'thumbnails'], item)
                obj._set_api_data(['title', 'channel', 'thumbnails'], item)
            return obj

        try:
            if api_enabled:
                data = API.search(q)
            else:
                data = scrAPI.search(q)
        except Exception as e:
            logger.error('search error "%s"', e)
            return None
            
        try:
            mapped_return = map(create_object, data['items'])
        except Exception as e:
            logger.error('map error "%s"', e)
            return None
            
        return mapped_return 

    # Adds futures for the given fields to all objects in list, unless they
    # already exist. Returns objects for which at least one future was added
    #
    @classmethod
    def _add_futures(cls, list, fields):
        def add(obj):
            added = False
            for k in fields:
                if '_'+k not in obj.__dict__:
                    obj.__dict__['_'+k] = pykka.ThreadingFuture()
                    added = True
            return added

        return filter(add, list)

    # common Video/Playlist properties go to the base class
    #
    @async_property
    def title(self):
        self.load_info([self])

    @async_property
    def channel(self):
        self.load_info([self])

    # sets the given 'fields' of 'self', based on the 'item'
    # data retrieved through the API
    #
    def _set_api_data(self, fields, item):
        for k in fields:
            _k = '_' + k
            future = self.__dict__.get(_k)
            if not future:
                future = self.__dict__[_k] = pykka.ThreadingFuture()

            if not future._queue.empty():  # hack, no public is_set()
                continue

            if not item:
                val = None
            elif k == 'title':
                val = item['snippet']['title']
            elif k == 'channel':
                val = item['snippet']['channelTitle']
            elif k == 'length':
                # convert PT1H2M10S to 3730
                m = re.search('PT((?P<hours>\d+)H)?' +
                              '((?P<minutes>\d+)M)?' +
                              '((?P<seconds>\d+)S)?',
                              item['contentDetails']['duration'])
                val = (int(m.group('hours') or 0) * 3600 +
                       int(m.group('minutes') or 0) * 60 +
                       int(m.group('seconds') or 0))
            elif k == 'video_count':
                val = min(item['contentDetails']['itemCount'], self.max_videos)
            elif k == 'thumbnails':
                val = [
                    val['url']
                    for (key, val) in item['snippet']['thumbnails'].items()
                    if key in ['medium', 'high']
                ]

            future.set(val)


class Video(Entry):

    # loads title, length, channel of multiple videos using one API call for
    # every 50 videos. API calls are split in separate threads.
    #
    @classmethod
    def load_info(cls, list):
        fields = ['title', 'length', 'channel']
        list = cls._add_futures(list, fields)

        def job(sublist):
            try:
                if api_enabled:
                    data = API.list_videos([x.id for x in sublist])
                else:
                    data = scrAPI.list_videos([x.id for x in sublist])
                dict = {item['id']: item for item in data['items']}
            except Exception as e:
                logger.error('list_videos error "%s"', e)
                dict = {}

            for video in sublist:
                video._set_api_data(fields, dict.get(video.id))

        # 50 items at a time, make sure order is deterministic so that HTTP
        # requests are replayable in tests
        for i in range(0, len(list), 50):
            sublist = list[i:i+50]
            ThreadPool.run(job, (sublist,))

    @async_property
    def length(self):
        self.load_info([self])

    @async_property
    def thumbnails(self):
        # make it "async" for uniformity with Playlist.thumbnails
        self._thumbnails = pykka.ThreadingFuture()
        self._thumbnails.set([
            'https://i.ytimg.com/vi/%s/%s.jpg' % (self.id, type)
            for type in ['mqdefault', 'hqdefault']
        ])

    # audio_url is the only property retrived using youtube_dl, it's much more
    # expensive than the rest
    #
    @async_property
    def audio_url(self):
        self._audio_url = pykka.ThreadingFuture()

        def job():
            try:
                info = youtube_dl.YoutubeDL(
                    {'format': 'm4a/vorbis/bestaudio/best'}
                ).extract_info(
                    url = "https://www.youtube.com/watch?v=%s" % self.id,
                    download = False,
                    ie_key=None, 
                    extra_info={}, 
                    process=True, 
                    force_generic_extractor=False
                )
            except Exception as e:
                logger.error('audio_url error "%s"', e)
                self._audio_url.set(None)
                return

            # return aac stream (.m4a) cause gstreamer 0.10 has issues with ogg
            # containing opus format!
            #  test id: cF9z1b5HL7M, playback gives error:
            #   Could not find a audio/x-unknown decoder to handle media.
            #   You might be able to fix this by running: gst-installer
            #   "gstreamer|0.10|mopidy|audio/x-unknown
            #   decoder|decoder-audio/x-unknown, codec-id=(string)A_OPUS"
            #
            self._audio_url.set(info['url'])

        ThreadPool.run(job)

    @property
    def is_video(self):
        return True


class Playlist(Entry):
    # overridable by config
    max_videos = 60     # max number of videos per playlist

    # loads title, thumbnails, video_count, channel of multiple playlists using
    # one API call for every 50 lists. API calls are split in separate threads.
    #
    @classmethod
    def load_info(cls, list):
        fields = ['title', 'video_count', 'thumbnails', 'channel']
        list = cls._add_futures(list, fields)

        def job(sublist):
            try:
                if api_enabled:
                    data = API.list_playlists([x.id for x in sublist])
                else:
                    data = scrAPI.list_playlists([x.id for x in sublist])
                dict = {item['id']: item for item in data['items']}
            except:
                dict = {}

            for pl in sublist:
                pl._set_api_data(fields, dict.get(pl.id))

        # 50 items at a time, make sure order is deterministic so that HTTP
        # requests are replayable in tests
        for i in range(0, len(list), 50):
            sublist = list[i:i+50]
            ThreadPool.run(job, (sublist,))

    # loads the list of videos of a playlist using one API call for every 50
    # fetched videos. For every page fetched, Video.load_info is called to
    # start loading video info in a separate thread.
    #
    @async_property
    def videos(self):
        self._videos = pykka.ThreadingFuture()

        def job():
            all_videos = []
            page = ''
            while page is not None and len(all_videos) < self.max_videos:
                try:
                    max_results = min(self.max_videos - len(all_videos), 50)
                    if api_enabled:
                        data = API.list_playlistitems(self.id, page, max_results)
                    else:
                        data = scrAPI.list_playlistitems(self.id, page, max_results)
                except:
                    break
                page = data.get('nextPageToken') or None

                myvideos = []
                for item in data['items']:
                    video = Video.get(item['snippet']['resourceId']['videoId'])
                    video._set_api_data(['title'], item)
                    myvideos.append(video)
                all_videos += myvideos

                # start loading video info for this batch in the background
                Video.load_info(myvideos)

            self._videos.set(all_videos)

        ThreadPool.run(job)

    @async_property
    def video_count(self):
        self.load_info([self])

    @async_property
    def thumbnails(self):
        self.load_info([self])

    @property
    def is_video(self):
        return False


# Direct access to YouTube Data API
# https://developers.google.com/youtube/v3/docs/
#
class API:
    endpoint = 'https://www.googleapis.com/youtube/v3/'
    session = get_requests_session(
        proxy_config=config['proxy'],
        user_agent='%s/%s' % (
            mopidy_youtube.Extension.dist_name,
            mopidy_youtube.Extension.version)
        )

    # overridable by config
    search_results = 15
    key = 'none'

    # search for both videos and playlists using a single API call
    # https://developers.google.com/youtube/v3/docs/search
    #
    @classmethod
    def search(cls, q):
        query = {
            'part': 'id,snippet',
            'fields': 'items(id,snippet(title,thumbnails,channelTitle))',
            'maxResults': cls.search_results,
            'type': 'video,playlist',
            'q': q,
            'key': API.key
        }
        result = API.session.get(API.endpoint+'search', params=query)
        return result.json()

    # list videos
    # https://developers.google.com/youtube/v3/docs/videos/list
    @classmethod
    def list_videos(cls, ids):
        query = {
            'part': 'id,snippet,contentDetails',
            'fields': 'items(id,snippet(title,channelTitle),' +
                      'contentDetails(duration))',
            'id': ','.join(ids),
            'key': API.key
        }
        result = API.session.get(API.endpoint+'videos', params=query)
        return result.json()

    # list playlists
    # https://developers.google.com/youtube/v3/docs/playlists/list
    @classmethod
    def list_playlists(cls, ids):
        query = {
            'part': 'id,snippet,contentDetails',
            'fields': 'items(id,snippet(title,thumbnails,channelTitle),' +
                      'contentDetails(itemCount))',
            'id': ','.join(ids),
            'key': API.key
        }
        result = API.session.get(API.endpoint+'playlists', params=query)
        return result.json()

    # list playlist items
    # https://developers.google.com/youtube/v3/docs/playlistItems/list
    @classmethod
    def list_playlistitems(cls, id, page, max_results):
        query = {
            'part': 'id,snippet',
            'fields': 'nextPageToken,' +
                      'items(snippet(title,resourceId(videoId)))',
            'maxResults': max_results,
            'playlistId': id,
            'key': API.key,
            'pageToken': page,
        }
        result = API.session.get(API.endpoint+'playlistItems', params=query)
        return result.json()

# Indirect access to YouTube data, without API
#
class scrAPI:
    endpoint = 'https://www.youtube.com/'

    session = get_requests_session(
        proxy_config=config['proxy'],
        user_agent='%s/%s' % (
            mopidy_youtube.Extension.dist_name,
            mopidy_youtube.Extension.version)
        )

    # search for videos and playlists
    #
    @classmethod
    def search(cls, q):
        query = {
            # # get videos only
            # 'sp': 'EgIQAQ%253D%253D',
            'search_query': q.replace(' ','+')
        }

        result = scrAPI.session.get(scrAPI.endpoint+'results', params=query)
        regex = r'(?:video-count.*<b>(?:(?P<itemCount>[0-9]+)</b>)?(.|\n)*?)?<a href="/watch\?v=(?P<id>.{11})(?:&amp;list=(?P<playlist>PL.{32}))?" class=".*?" data-sessionlink=".*?"  title="(?P<title>.+?)" .+?((?:Duration: (?:(?P<durationHours>[0-9]+):)?(?P<durationMinutes>[0-9]+):(?P<durationSeconds>[0-9]{2}).</span>.*?)?<a href="(?P<uploaderUrl>/(?:user|channel)/[^"]+)"[^>]+>(?P<uploader>.*?)</a>.*?class="(yt-lockup-description|yt-uix-sessionlink)[^>]*>(?P<description>.*?))?</div>'
        items = []

        for match in re.finditer(regex, result.text):
            duration = ''
            if match.group('durationHours') != None:
                duration += match.group('durationHours')+'H'
            if match.group('durationMinutes') != None:
                duration += match.group('durationMinutes')+'M'
            if match.group('durationSeconds') != None:
                duration += match.group('durationSeconds')+'S'
            if match.group('playlist') != None:
                item = {
                    'id': {
                      'kind': 'youtube#playlist',
                      'playlistId': match.group('playlist')
                    },
                }
            else:
                item = {
                    'id': {
                      'kind': 'youtube#video',
                      'videoId': match.group('id')
                    },
                }
            if duration != '':
                item.update ({
                    'contentDetails': {
                        'duration': 'PT'+duration,
                    },
                })
            if match.group('itemCount') != None:
                item.update ({
                    'contentDetails': {
                        'itemCount': match.group('itemCount'),
                    },
                })
            item.update ({
                'snippet': {
                      'title': match.group('title'),
                      # TODO: full support for thumbnails
                      'thumbnails': {
                          'default': {
                              'url': 'https://i.ytimg.com/vi/'+match.group('id')+'/default.jpg',
                              'width': 120,
                              'height': 90,
                          },
                      },
                      'channelTitle': match.group('uploader'),
                },
            })
            items.append(item)
        return json.loads(json.dumps({'items': items}, sort_keys=False, indent=1))

    # list videos
    # 
    @classmethod
    def list_videos(cls, ids):

        regex = r'<div id="watch7-content"(?:.|\n)*?<meta itemprop="name" content="(?P<title>.*?)(?:">)(?:.|\n)*?<meta itemprop="duration" content="(?P<duration>.*?)(?:">)(?:.|\n)*?<link itemprop="url" href="http://www.youtube.com/(?:user|channel)/(?P<channelTitle>.*?)(?:">)(?:.|\n)*?</div>'
        items = []
        
        for id in ids:
            query = {
                'v': id,
            }
            result = scrAPI.session.get(scrAPI.endpoint+'watch', params=query)
            for match in re.finditer(regex, result.text):
                item = {
                    'id': id,
                    'snippet': {
                        'title': match.group('title'),
                        'channelTitle': match.group('channelTitle'),
                    },
                    'contentDetails': {
                        'duration': match.group('duration'),
                    }
                }
                items.append(item)
        return json.loads(json.dumps({'items': items}, sort_keys=False, indent=1))

    # list playlists
    # 
    @classmethod
    def list_playlists(cls, ids):

        regex = r'<div id="pl-header"(?:.|\n)*?"(?P<thumbnail>https://i\.ytimg\.com\/vi\/.{11}/).*?\.jpg(?:(.|\n))*?(?:.|\n)*?class="pl-header-title"(?:.|\n)*?\>\s*(?P<title>.*)(?:.|\n)*?<a href="/(user|channel)/(?:.|\n)*? >(?P<channelTitle>.*?)</a>(?:.|\n)*?(?P<itemCount>\d*) videos</li>'
        items = []

        for id in ids:
            query = {
                'list': id,
            }
            result = scrAPI.session.get(scrAPI.endpoint+'playlist', params=query)
            for match in re.finditer(regex, result.text):
                item = {
                    'id': id,
                    'snippet': {
                        'title': match.group('title'),
                        'channelTitle': match.group('channelTitle'),
                        'thumbnails': {
                            'default': {
                                'url': match.group('thumbnail')+'default.jpg',
                                'width': 120,
                                'height': 90,
                            },
                        },
                    },
                    'contentDetails': {
                        'itemCount': match.group('itemCount'),
                    }
                }
                items.append(item)
        return json.loads(json.dumps({'items': items}, sort_keys=False, indent=1))
        
    # list playlist items
    # 
    @classmethod
    def list_playlistitems(cls, id, page, max_results):
        
        query = {
            'list': id
        }

        result = scrAPI.session.get(scrAPI.endpoint+'playlist', params=query)
        regex = r'" data-title="(?P<title>.+?)".*?<a href="/watch\?v=(?P<id>.{11})\&amp;'
        items = []

        for match in islice(re.finditer(regex, result.text), max_results):
            item = {
                'snippet': {
                    'resourceId': {
                        'videoId': match.group('id'),
                        },
                    'title' : match.group('title'),
                },
            }
            items.append(item)
        return json.loads(json.dumps({'nextPageToken': None, 'items': items}, sort_keys=False, indent=1))

# simple 'dynamic' thread pool. Threads are created when new jobs arrive, stay
# active for as long as there are active jobs, and get destroyed afterwards
# (so that there are no long-term threads staying active)
#
class ThreadPool:
    threads_max = 2
    threads_active = 0
    jobs = []
    lock = threading.Lock()     # controls access to threads_active and jobs

    @classmethod
    def worker(cls):
        while True:
            cls.lock.acquire()
            if len(cls.jobs):
                f, args = cls.jobs.pop()
            else:
                # no more jobs, exit thread
                cls.threads_active -= 1
                cls.lock.release()
                break
            cls.lock.release()

            try:
                apply(f, args)
            except Exception as e:
                logger.error('youtube thread error: %s\n%s',
                             e, traceback.format_exc())

    @classmethod
    def run(cls, f, args=()):
        cls.lock.acquire()

        cls.jobs.append((f, args))

        if cls.threads_active < cls.threads_max:
            thread = threading.Thread(target=cls.worker)
            thread.daemon = True
            thread.start()
            cls.threads_active += 1

        cls.lock.release()
