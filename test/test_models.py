# coding=utf-8
"""Unit tests for models.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import datetime
import json
import re

from granary import source as gr_source
import mox

import blogger
import facebook
import flickr
import googleplus
import instagram
import models
from models import BlogPost, Response, Source, SyndicatedPost
import superfeedr
import testutil
from testutil import FakeGrSource, FakeSource
import tumblr
import twitter
import util
import wordpress_rest


class ResponseTest(testutil.ModelsTest):

  def assert_propagate_task(self):
    tasks = self.taskqueue_stub.GetTasks('propagate')
    self.assertEqual(1, len(tasks))
    self.assertEqual(self.responses[0].key.urlsafe(),
                     testutil.get_task_params(tasks[0])['response_key'])
    self.assertEqual('/_ah/queue/propagate', tasks[0]['url'])
    self.taskqueue_stub.FlushQueue('propagate')

  def assert_no_propagate_task(self):
    self.assertEqual(0, len(self.taskqueue_stub.GetTasks('propagate')))

  def test_get_or_save(self):
    response = self.responses[0]
    self.assertEqual(0, Response.query().count())
    self.assert_no_propagate_task()

    # new. should add a propagate task.
    saved = response.get_or_save(self.sources[0])
    self.assertEqual(response.key, saved.key)
    self.assertEqual(response.source, saved.source)
    self.assertEqual('comment', saved.type)
    self.assertEqual([], saved.old_response_jsons)
    self.assert_propagate_task()

    # existing. no new task.
    same = saved.get_or_save(self.sources[0])
    self.assert_entities_equal(saved, same)
    self.assert_no_propagate_task()

  def test_get_or_save_activity_changed(self):
    """If the response activity has changed, we should update and resend."""
    # original response
    response = self.responses[0]
    response.put()
    self.assert_no_propagate_task()

    # change response content
    old_resp_json = response.response_json
    new_resp_json = json.loads(old_resp_json)
    new_resp_json['content'] = 'new content'
    response.response_json = json.dumps(new_resp_json)

    response = response.get_or_save(self.sources[0])
    self.assert_equals(json.dumps(new_resp_json), response.response_json)
    self.assert_equals([old_resp_json], response.old_response_jsons)
    self.assert_propagate_task()

    # mark response completed, change content again
    def complete():
      response.unsent = []
      response.sent = ['http://sent']
      response.error = ['http://error']
      response.failed = ['http://failed']
      response.skipped = ['http://skipped']
      response.status = 'complete'
      response.put()

    complete()
    newer_resp_json = json.loads(response.response_json)
    newer_resp_json['content'] = 'newer content'
    response.response_json = json.dumps(newer_resp_json)

    response = response.get_or_save(self.sources[0])
    self.assert_equals(json.dumps(newer_resp_json), response.response_json)
    self.assert_equals([old_resp_json, json.dumps(new_resp_json)],
                       response.old_response_jsons)
    self.assertEqual('new', response.status)
    urls = ['http://sent', 'http://error', 'http://failed', 'http://skipped']
    self.assertItemsEqual(urls, response.unsent)
    for field in response.sent, response.error, response.failed, response.skipped:
      self.assertEqual([], field)
    self.assert_propagate_task()

    # change Response.type
    complete()
    response.type = 'rsvp'
    response = response.get_or_save(self.sources[0])
    self.assertEqual('new', response.status)
    self.assertItemsEqual(urls, response.unsent)
    for field in response.sent, response.error, response.failed, response.skipped:
      self.assertEqual([], field)
    self.assert_propagate_task()

  def test_get_or_save_objectType_note(self):
    self.responses[0].response_json = json.dumps({
      'objectType': 'note',
      'id': 'tag:source.com,2013:1_2_%s' % id,
      })
    saved = self.responses[0].get_or_save(self.sources[0])
    self.assertEqual('comment', saved.type)

  def test_url(self):
    self.assertEqual('http://localhost/fake/%s' % self.sources[0].key.string_id(),
                     self.sources[0].bridgy_url(self.handler))

  def test_get_or_save_empty_unsent_no_task(self):
    self.responses[0].unsent = []
    saved = self.responses[0].get_or_save(self.sources[0])
    self.assertEqual('complete', saved.status)
    self.assert_no_propagate_task()

  def test_get_type(self):
    self.assertEqual('repost', Response.get_type(
        {'objectType': 'activity', 'verb': 'share'}))
    self.assertEqual('rsvp', Response.get_type({'verb': 'rsvp-no'}))
    self.assertEqual('rsvp', Response.get_type({'verb': 'invite'}))
    self.assertEqual('comment', Response.get_type({'objectType': 'comment'}))
    self.assertEqual('post', Response.get_type({'verb': 'post'}))
    self.assertEqual('post', Response.get_type({'objectType': 'event'}))
    self.assertEqual('post', Response.get_type({'objectType': 'image'}))
    self.assertEqual('comment', Response.get_type({
      'objectType': 'note',
      'context': {'inReplyTo': {'foo': 'bar'}},
    }))
    self.assertEqual('comment', Response.get_type({
      'objectType': 'comment',
      'verb': 'post',
    }))


class SourceTest(testutil.HandlerTest):

  def test_sources_global(self):
    self.assertEquals(blogger.Blogger, models.sources['blogger'])
    self.assertEquals(facebook.FacebookPage, models.sources['facebook'])
    self.assertEquals(flickr.Flickr, models.sources['flickr'])
    self.assertEquals(googleplus.GooglePlusPage, models.sources['googleplus'])
    self.assertEquals(instagram.Instagram, models.sources['instagram'])
    self.assertEquals(tumblr.Tumblr, models.sources['tumblr'])
    self.assertEquals(twitter.Twitter, models.sources['twitter'])
    self.assertEquals(wordpress_rest.WordPress, models.sources['wordpress'])

  def _test_create_new(self, **kwargs):
    FakeSource.create_new(self.handler, domains=['foo'],
                          domain_urls=['http://foo.com'],
                          webmention_endpoint='http://x/y',
                          **kwargs)
    self.assertEqual(1, FakeSource.query().count())

    tasks = self.taskqueue_stub.GetTasks('poll')
    self.assertEqual(1, len(tasks))
    source = FakeSource.query().get()
    self.assertEqual('/_ah/queue/poll', tasks[0]['url'])
    self.assertEqual(source.key.urlsafe(),
                     testutil.get_task_params(tasks[0])['source_key'])
    self.assertEqual('fake (FakeSource)', source.label())

  def test_create_new(self):
    self.assertEqual(0, FakeSource.query().count())
    self._test_create_new(features=['listen'])
    msg = "Added fake (FakeSource). Refresh in a minute to see what we've found!"
    self.assert_equals({msg}, self.handler.messages)

    for queue in 'poll', 'poll-now':
      task_params = testutil.get_task_params(self.taskqueue_stub.GetTasks(queue)[0])
      self.assertEqual('1970-01-01-00-00-00', task_params['last_polled'])

  def test_get_activities_injects_web_site_urls_into_user_mentions(self):
    source = FakeSource.new(None, domain_urls=['http://site1/', 'http://site2/'])
    source.put()

    mention = {
      'object': {
        'tags': [{
          'objectType': 'person',
          'id': 'tag:fa.ke,2013:%s' % source.key.id(),
          'url': 'https://fa.ke/me',
        }, {
          'objectType': 'person',
          'id': 'tag:fa.ke,2013:bob',
        }],
      },
    }
    FakeGrSource.activities = [mention]

    # check that we inject their web sites
    got = super(FakeSource, source).get_activities_response()
    mention['object']['tags'][0]['urls'] = [
      {'value': 'http://site1/'}, {'value': 'http://site2/'}]
    self.assert_equals([mention], got['items'])

  def test_get_comment_injects_web_site_urls_into_user_mentions(self):
    source = FakeSource.new(None, domain_urls=['http://site1/', 'http://site2/'])
    source.put()

    user_id = 'tag:fa.ke,2013:%s' % source.key.id()
    FakeGrSource.comment = {
      'id': 'tag:fa.ke,2013:a1-b2.c3',
      'tags': [
        {'id': 'tag:fa.ke,2013:nobody'},
        {'id': user_id},
      ],
    }

    # check that we inject their web sites
    self.assert_equals({
      'id': 'tag:fa.ke,2013:%s' % source.key.id(),
      'urls': [{'value': 'http://site1/'}, {'value': 'http://site2/'}],
    }, super(FakeSource, source).get_comment('x')['tags'][1])

  def test_create_new_already_exists(self):
    long_ago = datetime.datetime(year=1901, month=2, day=3)
    props = {
      'created': long_ago,
      'last_webmention_sent': long_ago + datetime.timedelta(days=1),
      'last_polled': long_ago + datetime.timedelta(days=2),
      'last_hfeed_refetch': long_ago + datetime.timedelta(days=3),
      'last_syndication_url': long_ago + datetime.timedelta(days=4),
      'superfeedr_secret': 'asdfqwert',
      }
    FakeSource.new(None, features=['listen'], **props).put()
    self.assert_equals(['listen'], FakeSource.query().get().features)

    FakeSource.string_id_counter -= 1
    auth_entity = testutil.FakeAuthEntity(
      id='x', user_json=json.dumps({'url': 'http://foo.com/'}))
    auth_entity.put()
    self._test_create_new(auth_entity=auth_entity, features=['publish'])

    source = FakeSource.query().get()
    self.assert_equals(['listen', 'publish'], source.features)
    for prop, value in props.items():
      self.assert_equals(value, getattr(source, prop), prop)

    self.assert_equals(
      {"Updated fake (FakeSource). Try previewing a post from your web site!"},
      self.handler.messages)

    task_params = testutil.get_task_params(self.taskqueue_stub.GetTasks('poll')[0])
    self.assertEqual('1901-02-05-00-00-00', task_params['last_polled'])

  def test_create_new_publish(self):
    """If a source is publish only, we shouldn't insert a poll task."""
    FakeSource.create_new(self.handler, features=['publish'])
    self.assertEqual(0, len(self.taskqueue_stub.GetTasks('poll')))
    self.assertEqual(0, len(self.taskqueue_stub.GetTasks('poll-now')))

  def test_create_new_webmention(self):
    """We should subscribe to webmention sources in Superfeedr."""
    self.expect_webmention_requests_get('http://primary/', 'no webmention endpoint',
                                        verify=False)
    self.mox.StubOutWithMock(superfeedr, 'subscribe')
    superfeedr.subscribe(mox.IsA(FakeSource), self.handler)

    self.mox.ReplayAll()
    FakeSource.create_new(self.handler, features=['webmention'],
                          domains=['primary/'], domain_urls=['http://primary/'])

  def test_create_new_domain(self):
    """If the source has a URL set, extract its domain."""
    # bad URLs
    for user_json in (None, {}, {'url': 'not<a>url'},
                      # t.co is in the webmention blacklist
                      {'url': 'http://t.co/foo'}):
      auth_entity = None
      if user_json is not None:
        auth_entity = testutil.FakeAuthEntity(id='x', user_json=json.dumps(user_json))
        auth_entity.put()
      source = FakeSource.create_new(self.handler, auth_entity=auth_entity)
      self.assertEqual([], source.domains)
      self.assertEqual([], source.domain_urls)

    # good URLs
    for url in ('http://foo.com/bar', 'https://www.foo.com/bar',
                'http://FoO.cOm/',  # should be normalized to lowercase
                ):
      auth_entity = testutil.FakeAuthEntity(
        id='x', user_json=json.dumps({'url': url}))
      auth_entity.put()
      source = FakeSource.create_new(self.handler, auth_entity=auth_entity)
      self.assertEquals([url.lower()], source.domain_urls)
      self.assertEquals(['foo.com'], source.domains)

    # multiple good URLs and one that's in the webmention blacklist
    auth_entity = testutil.FakeAuthEntity(id='x', user_json=json.dumps({
          'url': 'http://foo.org',
          'urls': [{'value': u} for u in
                   'http://bar.com', 'http://t.co/x', 'http://baz',
                   # utm_* query params should be stripped
                   'https://baj/biff?utm_campaign=x&utm_source=y'],
          }))
    auth_entity.put()
    source = FakeSource.create_new(self.handler, auth_entity=auth_entity)
    self.assertEquals(['http://foo.org/', 'http://bar.com/', 'http://baz/',
                       'https://baj/biff'],
                      source.domain_urls)
    self.assertEquals(['foo.org', 'bar.com', 'baz', 'baj'], source.domains)

    # a URL that redirects
    auth_entity = testutil.FakeAuthEntity(
      id='x', user_json=json.dumps({'url': 'http://orig'}))
    auth_entity.put()

    self.expect_requests_head('http://orig', redirected_url='http://final')
    self.mox.ReplayAll()

    source = FakeSource.create_new(self.handler, auth_entity=auth_entity)
    self.assertEquals(['http://final/'], source.domain_urls)
    self.assertEquals(['final'], source.domains)

  def test_create_new_unicode_chars(self):
    """We should handle unusual unicode chars in the source's name ok."""
    # the invisible character in the middle is an unusual unicode character
    FakeSource.create_new(self.handler, name=u'a ✁ b')

  def test_create_new_rereads_domains(self):
    FakeSource.new(None, features=['listen'],
                   domain_urls=['http://foo'], domains=['foo']).put()

    FakeSource.string_id_counter -= 1
    auth_entity = testutil.FakeAuthEntity(id='x', user_json=json.dumps(
        {'urls': [{'value': 'http://bar'}, {'value': 'http://baz'}]}))
    self.expect_webmention_requests_get('http://bar/', 'no webmention endpoint',
                                        verify=False)

    self.mox.ReplayAll()
    source = FakeSource.create_new(self.handler, auth_entity=auth_entity)
    self.assertEquals(['http://bar/', 'http://baz/'], source.domain_urls)
    self.assertEquals(['bar', 'baz'], source.domains)

  def test_create_new_dedupes_domains(self):
    auth_entity = testutil.FakeAuthEntity(id='x', user_json=json.dumps(
        {'urls': [{'value': 'http://foo'},
                  {'value': 'https://foo/'},
                  {'value': 'http://foo/'},
                  {'value': 'http://foo'},
                ]}))
    self.mox.ReplayAll()
    source = FakeSource.create_new(self.handler, auth_entity=auth_entity)
    self.assertEquals(['https://foo/'], source.domain_urls)
    self.assertEquals(['foo'], source.domains)

  def test_create_new_too_many_domains(self):
    urls = ['http://%s/' % i for i in range(10)]
    auth_entity = testutil.FakeAuthEntity(id='x', user_json=json.dumps(
        {'urls': [{'value': u} for u in urls]}))

    # we should only check the first 5
    for url in urls[:models.MAX_AUTHOR_URLS]:
      self.expect_requests_head(url)
    self.mox.ReplayAll()

    source = FakeSource.create_new(self.handler, auth_entity=auth_entity)
    self.assertEquals(urls, source.domain_urls)
    self.assertEquals([str(i) for i in range(10)], source.domains)

  def test_verify(self):
    # this requests.get is called by webmention-tools
    self.expect_webmention_requests_get('http://primary/', """
<html><meta>
<link rel="webmention" href="http://web.ment/ion">
</meta></html>""", verify=False)
    self.mox.ReplayAll()

    source = FakeSource.new(self.handler, features=['webmention'],
                            domain_urls=['http://primary/'], domains=['primary'])
    source.verify()
    self.assertEquals('http://web.ment/ion', source.webmention_endpoint)

  def test_verify_unicode_characters(self):
    """Older versions of BS4 had an issue where it would check short HTML
    documents to make sure the user wasn't accidentally passing a URL,
    but converting the utf-8 document to ascii caused exceptions in some cases.
    """
    # this requests.get is called by webmention-tools
    self.expect_webmention_requests_get(
      'http://primary/', """\xef\xbb\xbf<html><head>
<link rel="webmention" href="http://web.ment/ion"></head>
</html>""", verify=False)
    self.mox.ReplayAll()

    source = FakeSource.new(self.handler, features=['webmention'],
                            domain_urls=['http://primary/'],
                            domains=['primary'])
    source.verify()
    self.assertEquals('http://web.ment/ion', source.webmention_endpoint)

  def test_verify_without_webmention_endpoint(self):
    self.expect_webmention_requests_get(
      'http://primary/', 'no webmention endpoint here!', verify=False)
    self.mox.ReplayAll()

    source = FakeSource.new(self.handler, features=['webmention'],
                            domain_urls=['http://primary/'], domains=['primary'])
    source.verify()
    self.assertIsNone(source.webmention_endpoint)

  def test_has_bridgy_webmention_endpoint(self):
    source = FakeSource.new(None)
    for endpoint, has in ((None, False),
                          ('http://foo', False ),
                          ('https://brid.gy/webmention/fake', True),
                          ('https://www.brid.gy/webmention/fake', True),
                          ):
      source.webmention_endpoint = endpoint
      self.assertEquals(has, source.has_bridgy_webmention_endpoint(), endpoint)

  def test_put_updates(self):
    source = FakeSource.new(None)
    source.put()
    updates = source.updates = {'status': 'disabled'}

    try:
      # check that source.updates is preserved through pre-put hook since some
      # Source subclasses (e.g. FacebookPage) use it.
      FakeSource._pre_put_hook = lambda fake: self.assertEquals(updates, fake.updates)
      Source.put_updates(source)
      self.assertEquals('disabled', source.key.get().status)
    finally:
      del FakeSource._pre_put_hook

  def test_should_refetch(self):
    source = FakeSource.new(None)  # haven't found a synd url yet
    self.assertFalse(source.should_refetch())

    source.last_hfeed_refetch = models.REFETCH_HFEED_TRIGGER  # override
    self.assertTrue(source.should_refetch())

    source.last_syndication_url = source.last_hfeed_refetch = testutil.NOW  # too soon
    self.assertFalse(source.should_refetch())

    source.last_poll_attempt = testutil.NOW  # too soon
    self.assertFalse(source.should_refetch())

    hour = datetime.timedelta(hours=1)
    source.last_hfeed_refetch -= (Source.FAST_REFETCH + hour)
    self.assertTrue(source.should_refetch())

    source.last_syndication_url -= datetime.timedelta(days=15)  # slow refetch
    self.assertFalse(source.should_refetch())

    source.last_hfeed_refetch -= (Source.SLOW_REFETCH + hour)
    self.assertTrue(source.should_refetch())

  def test_is_beta_user(self):
    source = FakeSource.new(self.handler)
    self.assertFalse(source.is_beta_user())

    self.mox.stubs.Set(util, 'BETA_USER_PATHS', set())
    self.assertFalse(source.is_beta_user())

    self.mox.stubs.Set(util, 'BETA_USER_PATHS', set([source.bridgy_path()]))
    self.assertTrue(source.is_beta_user())


class BlogPostTest(testutil.ModelsTest):

  def test_label(self):
    for feed_item in None, {}:
      bp = BlogPost(id='x')
      bp.put()
      self.assertEquals('BlogPost x [no url]', bp.label())

    bp = BlogPost(id='x', feed_item={'permalinkUrl': 'http://perma/link'})
    bp.put()
    self.assertEquals('BlogPost x http://perma/link', bp.label())


class SyndicatedPostTest(testutil.ModelsTest):

  def setUp(self):
    super(SyndicatedPostTest, self).setUp()

    self.source = FakeSource.new(None)
    self.source.put()

    self.relationships = []
    self.relationships.append(
        SyndicatedPost(parent=self.source.key,
                       original='http://original/post/url',
                       syndication='http://silo/post/url'))
    # two syndication for the same original
    self.relationships.append(
        SyndicatedPost(parent=self.source.key,
                       original='http://original/post/url',
                       syndication='http://silo/another/url'))
    # two originals for the same syndication
    self.relationships.append(
        SyndicatedPost(parent=self.source.key,
                       original='http://original/another/post',
                       syndication='http://silo/post/url'))
    self.relationships.append(
        SyndicatedPost(parent=self.source.key,
                       original=None,
                       syndication='http://silo/no-original'))
    self.relationships.append(
        SyndicatedPost(parent=self.source.key,
                       original='http://original/no-syndication',
                       syndication=None))

    for r in self.relationships:
      r.put()

  def test_insert_replaces_blanks(self):
    """Make sure we replace original=None with original=something
    when it is discovered"""

    # add a blank for the original too
    SyndicatedPost.insert_original_blank(
      self.source, 'http://original/newly-discovered')

    self.assertTrue(
      SyndicatedPost.query(
        SyndicatedPost.syndication == 'http://silo/no-original',
        SyndicatedPost.original == None, ancestor=self.source.key).get())

    self.assertTrue(
      SyndicatedPost.query(
        SyndicatedPost.original == 'http://original/newly-discovered',
        SyndicatedPost.syndication == None, ancestor=self.source.key).get())

    r = SyndicatedPost.insert(
        self.source, 'http://silo/no-original',
        'http://original/newly-discovered')
    self.assertIsNotNone(r)
    self.assertEquals('http://original/newly-discovered', r.original)

    # make sure it's in NDB
    rs = SyndicatedPost.query(
        SyndicatedPost.syndication == 'http://silo/no-original',
        ancestor=self.source.key
    ).fetch()
    self.assertEquals(1, len(rs))
    self.assertEquals('http://original/newly-discovered', rs[0].original)
    self.assertEquals('http://silo/no-original', rs[0].syndication)

    # and the blanks have been removed
    self.assertFalse(
      SyndicatedPost.query(
        SyndicatedPost.syndication == 'http://silo/no-original',
        SyndicatedPost.original == None, ancestor=self.source.key).get())

    self.assertFalse(
      SyndicatedPost.query(
        SyndicatedPost.original == 'http://original/newly-discovered',
        SyndicatedPost.syndication == None, ancestor=self.source.key).get())

  def test_insert_auguments_existing(self):
    """Make sure we add newly discovered urls for a given syndication url,
    rather than overwrite them
    """
    r = SyndicatedPost.insert(
        self.source, 'http://silo/post/url',
        'http://original/different/url')
    self.assertIsNotNone(r)
    self.assertEquals('http://original/different/url', r.original)

    # make sure they're both in the DB
    rs = SyndicatedPost.query(
        SyndicatedPost.syndication == 'http://silo/post/url',
        ancestor=self.source.key
    ).fetch()

    self.assertItemsEqual(['http://original/post/url',
                           'http://original/another/post',
                           'http://original/different/url'],
                          [rel.original for rel in rs])

  def test_get_or_insert_by_syndication_do_not_duplicate_blanks(self):
    """Make sure we don't insert duplicate blank entries"""

    SyndicatedPost.insert_syndication_blank(
      self.source, 'http://silo/no-original')

    # make sure there's only one in the DB
    rs = SyndicatedPost.query(
        SyndicatedPost.syndication == 'http://silo/no-original',
        ancestor=self.source.key
    ).fetch()

    self.assertItemsEqual([None], [rel.original for rel in rs])

  def test_insert_no_duplicates(self):
    """Make sure we don't insert duplicate entries"""

    r = SyndicatedPost.insert(
      self.source, 'http://silo/post/url', 'http://original/post/url')
    self.assertIsNotNone(r)
    self.assertEqual('http://original/post/url', r.original)

    # make sure there's only one in the DB
    rs = SyndicatedPost.query(
      SyndicatedPost.syndication == 'http://silo/post/url',
      SyndicatedPost.original == 'http://original/post/url',
      ancestor=self.source.key
    ).fetch()

    self.assertEqual(1, len(rs))
