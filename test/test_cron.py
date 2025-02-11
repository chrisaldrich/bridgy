"""Unit tests for cron.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import copy
import datetime
import json

from granary.test import test_flickr
from granary.test import test_instagram
import oauth_dropins
from oauth_dropins import indieauth
from oauth_dropins import flickr as oauth_flickr

import cron
from flickr import Flickr
from instagram import Instagram
import testutil
from testutil import FakeSource, HandlerTest


class CronTest(HandlerTest):
  def setUp(self):
    super(CronTest, self).setUp()
    oauth_dropins.appengine_config.FLICKR_APP_KEY = 'my_app_key'
    oauth_dropins.appengine_config.FLICKR_APP_SECRET = 'my_app_secret'

    flickr_auth = oauth_flickr.FlickrAuth(
      id='123@N00', user_json=json.dumps(test_flickr.PERSON_INFO),
      token_key='my_key', token_secret='my_secret')
    flickr_auth.put()
    self.flickr = Flickr.new(None, auth_entity=flickr_auth, features=['listen'])
    self.assertEquals(
      'https://farm5.staticflickr.com/4068/buddyicons/39216764@N00.jpg',
      self.flickr.picture)

  def test_replace_poll_tasks(self):
    self.assertEqual([], self.taskqueue_stub.GetTasks('poll'))
    now = datetime.datetime.now()

    # a bunch of sources, one needs a new poll task
    five_min_ago = now - datetime.timedelta(minutes=5)
    day_and_half_ago = now - datetime.timedelta(hours=36)
    month_ago = now - datetime.timedelta(days=30)
    defaults = {
      'features': ['listen'],
      'last_webmention_sent': day_and_half_ago,
      }
    sources = [
      # doesn't need a new poll task
      FakeSource.new(None, last_poll_attempt=now, **defaults).put(),
      FakeSource.new(None, last_poll_attempt=five_min_ago, **defaults).put(),
      FakeSource.new(None, status='disabled', **defaults).put(),
      FakeSource.new(None, status='disabled', **defaults).put(),
      # need a new poll task
      FakeSource.new(None, status='enabled', **defaults).put(),
      # not signed up for listen
      FakeSource.new(None, last_webmention_sent=day_and_half_ago).put(),
      # never sent a webmention, past grace period. last polled is older than 2x
      # fast poll, but within 2x slow poll.
      FakeSource.new(None, features=['listen'], created=month_ago,
                     last_poll_attempt=day_and_half_ago).put(),
      ]
    resp = cron.application.get_response('/cron/replace_poll_tasks')
    self.assertEqual(200, resp.status_int)

    tasks = self.taskqueue_stub.GetTasks('poll')
    self.assertEqual(1, len(tasks))
    self.assert_equals(sources[4].urlsafe(),
                       testutil.get_task_params(tasks[0])['source_key'])

  def test_update_instagram_pictures(self):
    for username in 'a', 'b':
      profile = copy.deepcopy(test_instagram.HTML_PROFILE)
      profile['entry_data']['ProfilePage'][0]['user'].update({
        'username': username,
        'profile_pic_url': 'http://new/pic',
        })
      super(HandlerTest, self).expect_requests_get(
        'https://www.instagram.com/%s/' % username,
        test_instagram.HTML_HEADER + json.dumps(profile) + test_instagram.HTML_FOOTER,
        allow_redirects=False)
    self.mox.ReplayAll()

    sources = []
    auth_entity = indieauth.IndieAuth(id='http://foo.com/', user_json='{}')
    for username in 'a', 'b', 'c', 'd':
      source = Instagram.new(
        None, auth_entity=auth_entity, features=['listen'],
        actor={'username': username, 'image': {'url': 'http://old/pic'}})
      # test that we skip disabled and deleted sources
      if username == 'c':
        source.status = 'disabled'
      elif username == 'd':
        source.features = []
      sources.append(source.put())

    resp = cron.application.get_response('/cron/update_instagram_pictures')
    self.assertEqual(200, resp.status_int)

    self.assertEquals('http://new/pic', sources[0].get().picture)
    self.assertEquals('http://new/pic', sources[1].get().picture)
    self.assertEquals('http://old/pic', sources[2].get().picture)
    self.assertEquals('http://old/pic', sources[3].get().picture)

  def test_update_instagram_picture_profile_404s(self):
    auth_entity = indieauth.IndieAuth(id='http://foo.com/', user_json='{}')
    source = Instagram.new(
        None, auth_entity=auth_entity, features=['listen'],
        actor={'username': 'x', 'image': {'url': 'http://old/pic'}})
    source.put()

    super(HandlerTest, self).expect_requests_get(
      'https://www.instagram.com/x/', status_code=404, allow_redirects=False)
    self.mox.ReplayAll()

    resp = cron.application.get_response('/cron/update_instagram_pictures')
    self.assertEqual(200, resp.status_int)
    self.assertEquals('http://old/pic', source.key.get().picture)

  def test_update_flickr_pictures(self):
    self.expect_urlopen(
      'https://api.flickr.com/services/rest?'
        'user_id=39216764%40N00&nojsoncallback=1&'
        'method=flickr.people.getInfo&format=json',
      json.dumps({
        'person': {
          'id': '123@N00',
          'nsid': '123@N00',
          'iconfarm': 9,
          'iconserver': '9876',
        }}))
    self.mox.ReplayAll()

    self.flickr.put()
    resp = cron.application.get_response('/cron/update_flickr_pictures')
    self.assertEqual(200, resp.status_int)
    self.assertEquals(
      'https://farm9.staticflickr.com/9876/buddyicons/123@N00.jpg',
      self.flickr.key.get().picture)
