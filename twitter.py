"""Twitter source code and datastore model classes.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import datetime
import json

import webapp2

import appengine_config

from granary import twitter as gr_twitter
from granary import source as gr_source
from oauth_dropins import twitter as oauth_twitter
import models
import util
import logging


class Twitter(models.Source):
  """A Twitter account.

  The key name is the username.
  """

  GR_CLASS = gr_twitter.Twitter
  SHORT_NAME = 'twitter'
  TYPE_LABELS = {'post': 'tweet',
                 'comment': '@-reply',
                 'repost': 'retweet',
                 'like': 'favorite',
                 }

  URL_CANONICALIZER = util.UrlCanonicalizer(
    domain=GR_CLASS.DOMAIN,
    approve=r'https://twitter\.com/[^/?]+/status/[^/?]+',
    reject=r'https://twitter\.com/.+\?protected_redirect=true',
    headers=util.USER_AGENT_HEADER)

  # Twitter's rate limiting window is currently 15m. A normal poll with nothing
  # new hits /statuses/user_timeline and /search/tweets once each. Both
  # allow 180 calls per window before they're rate limited.
  # https://dev.twitter.com/docs/rate-limiting/1.1/limits

  @staticmethod
  def new(handler, auth_entity=None, **kwargs):
    """Creates and returns a Twitter entity.

    Args:
      handler: the current RequestHandler
      auth_entity: oauth-dropins.twitter.TwitterAuth
      kwargs: property values
    """
    user = json.loads(auth_entity.user_json)
    gr_source = gr_twitter.Twitter(*auth_entity.access_token())
    actor = gr_source.user_to_actor(user)
    return Twitter(id=user['screen_name'],
                   auth_entity=auth_entity.key,
                   url=actor.get('url'),
                   name=actor.get('displayName'),
                   picture=actor.get('image', {}).get('url'),
                   **kwargs)

  def silo_url(self):
    """Returns the Twitter account URL, e.g. https://twitter.com/foo."""
    return self.gr_source.user_url(self.key.id())

  def label_name(self):
    """Returns the username."""
    return self.key.id()

  def search_for_links(self):
    """Searches for activities with links to any of this source's web sites.

    Twitter search supports OR:
    https://dev.twitter.com/rest/public/search

    ...but it only returns complete(ish) results if we strip scheme from URLs,
    ie search for example.com instead of http://example.com/, and that also
    returns false positivies, so we check that the returned tweets actually have
    matching links. https://github.com/snarfed/bridgy/issues/565

    Returns: sequence of ActivityStreams activity dicts
    """
    urls = set(util.fragmentless(url) for url in self.domain_urls
               if not util.in_webmention_blacklist(util.domain_from_link(url)))
    if not urls:
      return []

    query = ' OR '.join('"%s"' % util.schemeless(url, slashes=False) for url in urls)
    candidates = self.get_activities(
      search_query=query, group_id=gr_source.SEARCH, etag=self.last_activities_etag,
      fetch_replies=False, fetch_likes=False, fetch_shares=False, count=50)

    # filter out retweets and search false positives that don't actually link to us
    results = []
    for candidate in candidates:
      if candidate.get('verb') == 'share':
        continue
      obj = candidate['object']
      tags = obj.get('tags', [])
      atts = obj.get('attachments', [])
      for url in urls:
        if (url in obj.get('content', '') or
            any(t.get('url', '').startswith(url) for t in tags + atts)):
          id = candidate['id']
          results.append(candidate)
          break

    return results

  def get_like(self, activity_user_id, activity_id, like_user_id):
    """Returns an ActivityStreams 'like' activity object for a favorite.

    We get Twitter favorites by scraping HTML, and we only get the first page,
    which only has 25. So, use a Response in the datastore first, if we have
    one, and only re-scrape HTML as a fallback.

    Args:
      activity_user_id: string id of the user who posted the original activity
      activity_id: string activity id
      like_user_id: string id of the user who liked the activity
    """
    id = self.gr_source.tag_uri('%s_favorited_by_%s' % (activity_id, like_user_id))
    resp = models.Response.get_by_id(id)
    if resp:
      return json.loads(resp.response_json)
    else:
      return super(Twitter, self).get_like(activity_user_id, activity_id,
                                           like_user_id)

  def is_private(self):
    """Returns True if this Twitter account is protected.

    https://dev.twitter.com/rest/reference/get/users/show#highlighter_25173
    https://support.twitter.com/articles/14016
    https://support.twitter.com/articles/20169886
    """
    return json.loads(self.auth_entity.get().user_json).get('protected')

  def canonicalize_url(self, url, activity=None, **kwargs):
    """Normalize /statuses/ to /status/.

    https://github.com/snarfed/bridgy/issues/618
    """
    url = url.replace('/statuses/', '/status/')
    return super(Twitter, self).canonicalize_url(url, **kwargs)


class AuthHandler(util.Handler):
  """Base OAuth handler class."""

  def start_oauth_flow(self, feature):
    """Redirects to Twitter's OAuth endpoint to start the OAuth flow.

    Args:
      feature: 'listen' or 'publish'
    """
    features = feature.split(',') if feature else []
    assert all(f in models.Source.FEATURES for f in features)

    # pass explicit 'write' instead of None for publish so that oauth-dropins
    # (and tweepy) don't use signin_with_twitter ie /authorize. this works
    # around a twitter API bug: https://dev.twitter.com/discussions/21281
    access_type = 'write' if 'publish' in features else 'read'
    handler = util.oauth_starter(oauth_twitter.StartHandler, feature=feature).to(
      '/twitter/add', access_type=access_type)(self.request, self.response)
    return handler.post()


class AddTwitter(oauth_twitter.CallbackHandler, AuthHandler):
  def finish(self, auth_entity, state=None):
    source = self.maybe_add_or_delete_source(Twitter, auth_entity, state)
    feature = self.decode_state_parameter(state).get('feature')

    if source is not None and feature == 'listen' and 'publish' in source.features:
      # if we were already signed up for publish, we had a read/write token.
      # when we sign up for listen, we use x_auth_access_type=read to request
      # just read permissions, which *demotes* us to a read only token! ugh.
      # so, do the whole oauth flow again to get a read/write token.
      logging.info('Restarting OAuth flow to get publish permissions.')
      source.features.remove('publish')
      source.put()
      return self.start_oauth_flow('publish')


class StartHandler(AuthHandler):
  """Custom OAuth start handler so we can use access_type=read for
  state=listen.

  Tweepy converts access_type to x_auth_access_type for Twitter's
  oauth/request_token endpoint. Details:
  https://dev.twitter.com/docs/api/1/post/oauth/request_token
  """
  def post(self):
    return self.start_oauth_flow(util.get_required_param(self, 'feature'))


application = webapp2.WSGIApplication([
    ('/twitter/start', StartHandler),
    ('/twitter/add', AddTwitter),
    ('/twitter/delete/finish', oauth_twitter.CallbackHandler.to('/delete/finish')),
    ('/twitter/publish/start', oauth_twitter.StartHandler.to(
      '/publish/twitter/finish')),
    ], debug=appengine_config.DEBUG)
