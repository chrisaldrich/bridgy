"""Facebook API code and datastore model classes.

TODO: use third_party_id if we ever need to store an FB user id anywhere else.

Example post ID and links
  id: 212038_10100823411129293  [USER-ID]_[POST-ID]
  API URL: https://graph.facebook.com/212038_10100823411094363
  Permalinks:
    https://www.facebook.com/10100823411094363
    https://www.facebook.com/212038/posts/10100823411094363
    https://www.facebook.com/photo.php?fbid=10100823411094363
  Local handler path: /post/facebook/212038/10100823411094363

Example comment ID and links
  id: 10100823411094363_10069288  [POST-ID]_[COMMENT-ID]
  API URL: https://graph.facebook.com/10100823411094363_10069288
  Permalink: https://www.facebook.com/10100823411094363&comment_id=10069288
  Local handler path: /comment/facebook/212038/10100823411094363_10069288
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import heapq
import itertools
import json
import logging
import urllib2
import urlparse

import appengine_config

from granary import facebook as gr_facebook
from oauth_dropins import facebook as oauth_facebook
from granary import source as gr_source
import models
import util

from google.appengine.ext import ndb
from google.appengine.ext.webapp import template
import webapp2

# https://developers.facebook.com/docs/reference/login/
LISTEN_SCOPES = [
  'user_website', 'user_status', 'user_posts', 'user_photos', 'user_events',
  'user_actions.news', 'manage_pages',
]
PUBLISH_SCOPES = [
  'user_website', 'publish_actions', 'rsvp_event', 'user_status',
  'user_photos', 'user_videos', 'user_events', 'user_likes',
]

# WARNING: this edge is deprecated in API v2.4 and will stop working in 2017.
# https://developers.facebook.com/docs/apps/changelog#v2_4_deprecations
API_EVENT_RSVPS = '%s/invited'

# https://developers.facebook.com/docs/graph-api/using-graph-api/#errors
DEAD_TOKEN_ERROR_CODES = frozenset((
  200,  # "Permissions error"
))
DEAD_TOKEN_ERROR_SUBCODES = frozenset((
  458,  # "The user has not authorized application 123"
  460,  # "The session has been invalidated because the user has changed the password"
))
DEAD_TOKEN_ERROR_MESSAGES = frozenset((
  'The user must be an administrator of the page in order to impersonate it.',
))

MAX_RESOLVED_OBJECT_IDS = 200
MAX_POST_PUBLICS = 200


class FacebookPage(models.Source):
  """A facebook profile or page.

  The key name is the facebook id.
  """

  GR_CLASS = gr_facebook.Facebook
  SHORT_NAME = 'facebook'

  URL_CANONICALIZER = util.UrlCanonicalizer(
    domain=GR_CLASS.DOMAIN,
    subdomain='www',
    query=True,
    approve=r'https://www\.facebook\.com/[^/?]+/posts/[^/?]+',
    headers=util.USER_AGENT_HEADER)
    # no reject regexp; non-private FB post URLs just 404

  # unique name used in FB URLs, e.g. facebook.com/[username]
  username = ndb.StringProperty()
  # inferred from syndication URLs if username isn't available
  inferred_username = ndb.StringProperty()
  # inferred application-specific user IDs (from other applications)
  inferred_user_ids = ndb.StringProperty(repeated=True)

  # maps string FB post id to string FB object id or None. background:
  # https://github.com/snarfed/bridgy/pull/513#issuecomment-149312879
  resolved_object_ids_json = ndb.TextProperty(compressed=True)
  # maps string FB post id to True or False for whether the post is public
  # or private. only contains posts with *known* privacy. background:
  # https://github.com/snarfed/bridgy/issues/633#issuecomment-198806909
  post_publics_json = ndb.TextProperty(compressed=True)

  @staticmethod
  def new(handler, auth_entity=None, **kwargs):
    """Creates and returns a FacebookPage for the logged in user.

    Args:
      handler: the current RequestHandler
      auth_entity: oauth_dropins.facebook.FacebookAuth
      kwargs: property values
    """
    user = json.loads(auth_entity.user_json)
    gr_source = gr_facebook.Facebook(auth_entity.access_token())
    actor = gr_source.user_to_actor(user)
    return FacebookPage(id=user['id'],
                        auth_entity=auth_entity.key,
                        name=actor.get('displayName'),
                        username=actor.get('username'),
                        picture=actor.get('image', {}).get('url'),
                        url=actor.get('url'),
                        **kwargs)

  @classmethod
  def lookup(cls, id):
    """Returns the entity with the given id or username."""
    return ndb.Key(cls, id).get() or cls.query(cls.username == id).get()

  def silo_url(self):
    """Returns the Facebook account URL, e.g. https://facebook.com/foo."""
    return self.gr_source.user_url(self.username or self.key.id())

  def get_activities_response(self, **kwargs):
    kwargs.setdefault('fetch_events', True)
    kwargs.setdefault('fetch_news', self.auth_entity.get().type == 'user')
    kwargs.setdefault('event_owner_id', self.key.id())

    try:
      activities = super(FacebookPage, self).get_activities_response(**kwargs)
    except urllib2.HTTPError as e:
      code, body = util.interpret_http_exception(e)
      # use a function so any new exceptions (JSON decoding, missing keys) don't
      # clobber the original exception so we can re-raise it below.
      def dead_token():
        try:
          err = json.loads(body)['error']
          return (err.get('code') in DEAD_TOKEN_ERROR_CODES or
                  err.get('error_subcode') in DEAD_TOKEN_ERROR_SUBCODES or
                  err.get('message') in DEAD_TOKEN_ERROR_MESSAGES)
        except:
          logging.exception("Couldn't determine whether token is still valid")
          return False

      if code == '401':
        if not dead_token():
          # ask the user to reauthenticate. if this API call fails, it will raise
          # urllib2.HTTPError instead of DisableSource, so that we don't disable
          # the source without notifying.
          self.gr_source.create_notification(
            self.key.id(),
            "Brid.gy's access to your account has expired. Click here to renew it now!",
            'https://brid.gy/facebook/start')
        raise models.DisableSource()

      raise

    # update the resolved_object_ids and post_publics caches
    def parsed_post_id(id):
      parsed = gr_facebook.Facebook.parse_id(id)
      return parsed.post if parsed.post else id

    resolved = self._load_cache('resolved_object_ids')
    for activity in activities['items']:
      obj = activity.get('object', {})
      obj_id = parsed_post_id(obj.get('fb_id'))
      ids = obj.get('fb_object_for_ids')
      if obj_id and ids:
        resolved[obj_id] = obj_id
        for id in ids:
          resolved[parsed_post_id(id)] = obj_id

    for activity in activities['items']:
      self.is_activity_public(activity)

    return activities

  def canonicalize_url(self, url, activity=None, **kwargs):
    """Facebook-specific standardization of syndicated urls. Canonical form is
    https://www.facebook.com/USERID/posts/POSTID

    Args:
      url: a string, the url of the syndicated content
      activity: the activity this URL came from. If it has an fb_object_id,
        we'll use that instead of fetching the post from Facebook
      kwargs: unused

    Return:
      a string, the canonical form of the syndication url
    """
    if util.domain_from_link(url) != self.gr_source.DOMAIN:
      return None

    def post_url(id):
      return 'https://www.facebook.com/%s/posts/%s' % (self.key.id(), id)

    parsed = urlparse.urlparse(url)
    params = urlparse.parse_qs(parsed.query)
    url_id = self.gr_source.post_id(url)

    ids = params.get('story_fbid') or params.get('fbid')
    if ids:
      url = post_url(ids[0])
    elif url_id:
      if parsed.path.startswith('/notes/'):
        url = post_url(url_id)
      else:
        object_id = self.cached_resolve_object_id(url_id, activity=activity)
        if object_id:
          url = post_url(object_id)

    for alternate_id in util.trim_nulls(itertools.chain(
       (self.username or self.inferred_username,), self.inferred_user_ids)):
      url = url.replace('facebook.com/%s/' % alternate_id,
                        'facebook.com/%s/' % self.key.id())

    return super(FacebookPage, self).canonicalize_url(url)

  def cached_resolve_object_id(self, post_id, activity=None):
    """Resolve a post id to its Facebook object id, if any.

    Wraps granary.facebook.Facebook.resolve_object_id() and uses
    self.resolved_object_ids_json as a cache.

    Args:
      post_id: string Facebook post id
      activity: optional AS activity representation of Facebook post

    Returns: string Facebook object id or None
    """
    parsed = gr_facebook.Facebook.parse_id(post_id)
    if parsed.post:
      post_id = parsed.post

    resolved = self._load_cache('resolved_object_ids')
    if post_id not in resolved:
      resolved[post_id] = self.gr_source.resolve_object_id(
        self.key.id(), post_id, activity=activity)

    return resolved[post_id]

  def is_activity_public(self, activity):
    """Returns True if the given activity is public, False otherwise.

    Uses the post_publics cache if we can't tell otherwise.
    """
    obj = activity.get('object', {})
    fb_id = activity.get('fb_id') or obj.get('fb_id')
    if fb_id and gr_source.object_type(activity) not in ('comment', 'like', 'share'):
      fb_id = self.cached_resolve_object_id(fb_id, activity=activity)

    post_publics = self._load_cache('post_publics')
    public = gr_source.Source.is_public(activity)

    if not fb_id:
      return public
    elif public is not None:
      post_publics[fb_id] = public    # write cache
      return public
    else:
      return post_publics.get(fb_id)  # read cache

  def _load_cache(self, name):
    """Loads resolved_object_ids_json or post_publics_json into self.updates."""
    assert name in ('resolved_object_ids', 'post_publics')
    field = getattr(self, name + '_json')

    if self.updates is None:
      self.updates = {}
    loaded = self.updates.setdefault(name, {})

    if not loaded and field:
      loaded = self.updates[name] = json.loads(field)
    return loaded

  def _save_cache(self, name):
    """Writes resolved_object_ids or post_publics from self.updates to _json."""
    if self.updates is None:
      return

    assert name in ('resolved_object_ids', 'post_publics')
    max = globals()['MAX_' + name.upper()]
    val = self.updates.get(name)
    if val:
      keep = heapq.nlargest(max,
        (int(id) if util.is_int(id) else id for id in val.keys()))
      setattr(self, name + '_json',
              json.dumps({str(id): val[str(id)] for id in keep}))

  def _pre_put_hook(self):
    """Encode the resolved_object_ids and post_publics fields from updates.

    ...and cap them at MAX_RESOLVED_OBJECT_IDS and MAX_POST_PUBLICS. Tries to
    keep the latest ones by assuming that ids are roughly monotonically
    increasing.
    """
    self._save_cache('resolved_object_ids')
    self._save_cache('post_publics')

  def infer_profile_url(self, url):
    """Find a Facebook profile URL (ideally the one with the user's numeric ID)

    Looks up existing sources by username, inferred username, and domain.

    Args:
      url: string, a person's URL

    Return:
      a string URL for their Facebook profile (or None)
    """
    domain = util.domain_from_link(url)
    if domain == self.gr_source.DOMAIN:
      username = urlparse.urlparse(url).path.strip('/')
      if '/' not in username:
        user = FacebookPage.query(ndb.OR(
          FacebookPage.username == username,
          FacebookPage.inferred_username == username)).get()
        if user:
          return self.gr_source.user_url(user.key.id())
    return super(FacebookPage, self).infer_profile_url(url)

  @ndb.transactional
  def on_new_syndicated_post(self, syndpost):
    """If this source has no username, try to infer one from a syndication URL.

    Args:
      syndpost: SyndicatedPost
    """
    url = syndpost.syndication
    if self.username or not url:
      return

    # FB usernames only have letters, numbers, and periods:
    # https://www.facebook.com/help/105399436216001
    author_id = self.gr_source.base_object({'object': {'url': url}})\
                              .get('author', {}).get('id')
    if author_id:
      if author_id != self.inferred_username and not util.is_int(author_id):
        logging.info('Inferring username %s from syndication url %s', author_id, url)
        self.inferred_username = author_id
        self.put()
        syndpost.syndication = self.canonicalize_url(syndpost.syndication)
      elif author_id != self.key.id() and author_id not in self.inferred_user_ids:
        logging.info('Inferring app-scoped user id %s from syndication url %s', author_id, url)
        self.inferred_user_ids = util.uniquify(self.inferred_user_ids + [author_id])
        self.put()
        syndpost.syndication = self.canonicalize_url(syndpost.syndication)


class AuthHandler(util.Handler):
  """Base OAuth handler class."""

  def finish_oauth_flow(self, auth_entity, state):
    """Adds or deletes a FacebookPage, or restarts OAuth to get publish permissions.

    Args:
      auth_entity: FacebookAuth
      state: encoded state string
    """
    if auth_entity is None:
      auth_entity_key = self.request.get('auth_entity_key')
      if auth_entity_key:
        auth_entity = ndb.Key(urlsafe=auth_entity_key).get()

    if state is None:
      state = self.request.get('state')
    state_obj = self.decode_state_parameter(state) if state else {}

    id = state_obj.get('id') or self.request.get('id')
    if id and auth_entity and id != auth_entity.key.id():
      auth_entity = auth_entity.for_page(id)
      auth_entity.put()

    source = self.maybe_add_or_delete_source(FacebookPage, auth_entity, state)

    # If we were already signed up for publish, we had an access token with publish
    # permissions. If we then go through the listen signup flow, we'll get a token
    # with just the listen permissions. In that case, do the whole OAuth flow again
    # to get a token with publish permissions again.
    feature = state_obj.get('feature')
    if source is not None and feature == 'listen' and 'publish' in source.features:
      logging.info('Restarting OAuth flow to get publish permissions.')
      source.features.remove('publish')
      source.put()
      start = util.oauth_starter(oauth_facebook.StartHandler,
                                 feature='publish', id=id)
      restart = start.to('/facebook/oauth_handler', scopes=PUBLISH_SCOPES)
      restart(self.request, self.response).post()


class AddFacebookPage(AuthHandler):
  def post(self, auth_entity=None, state=None):
    self.finish_oauth_flow(auth_entity, state)


class OAuthCallback(oauth_facebook.CallbackHandler, AuthHandler):
  """OAuth callback handler."""
  def finish(self, auth_entity, state=None):
    id = self.decode_state_parameter(state).get('id')

    if auth_entity and json.loads(auth_entity.pages_json) and not id:
      # this user has FB page(s), and we don't know whether they want to sign
      # themselves up or one of their pages, so ask them.
      vars = {
        'action': '/facebook/add',
        'state': state,
        'auth_entity_key': auth_entity.key.urlsafe(),
        'choices': [json.loads(auth_entity.user_json)] +
                   json.loads(auth_entity.pages_json),
        }
      logging.info('Rendering choose_facebook.html with %s', vars)
      self.response.headers['Content-Type'] = 'text/html'
      self.response.out.write(
        template.render('templates/choose_facebook.html', vars))
      return

    # this user has no FB page(s), or we know the one they want to sign up.
    self.finish_oauth_flow(auth_entity, state)


class StartHandler(util.Handler):
  """Custom handler that sets OAuth scopes based on the requested
  feature(s)
  """
  def post(self):
    features = self.request.get('feature')
    features = features.split(',') if features else []
    starter = util.oauth_starter(oauth_facebook.StartHandler).to(
      '/facebook/oauth_handler', scopes=sorted(set(
        (LISTEN_SCOPES if 'listen' in features else []) +
        (PUBLISH_SCOPES if 'publish' in features else []))))
    starter(self.request, self.response).post()


application = webapp2.WSGIApplication([
    ('/facebook/start', StartHandler),
    ('/facebook/oauth_handler', OAuthCallback),
    ('/facebook/add', AddFacebookPage),
    ('/facebook/delete/finish', oauth_facebook.CallbackHandler.to('/delete/finish')),
    ('/facebook/publish/start', oauth_facebook.StartHandler.to(
      '/publish/facebook/finish')),
    ], debug=appengine_config.DEBUG)
