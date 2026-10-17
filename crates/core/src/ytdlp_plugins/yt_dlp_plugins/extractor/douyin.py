# Temporary compatibility patch from yt-dlp PR #17826 (Unlicense):
# https://github.com/yt-dlp/yt-dlp/pull/17826
# Upstream commit edf5ca18f756b1a21f56c87aaf1aef7ef8d3520e.
# Remove this override once the bundled engine includes the upstream fix.
import hashlib
import json
import time
import urllib.parse
import uuid

from yt_dlp.extractor.tiktok import DouyinIE as _DouyinIE
from yt_dlp.utils import ExtractorError
from yt_dlp.utils.traversal import traverse_obj


class DouyinIE(_DouyinIE, plugin_name='huifa_compat'):
    _WEBPAGE_HOST = 'https://www.douyin.com/'
    _SEC_SALT = 'A96D855A08C0A9707F8BEF0D9A527E4E'
    _REQUIRED_COOKIES = ('passport_csrf_token', 'ttwid')
    _AID = 6383
    _CSRF = uuid.uuid4().hex

    def _get_ttwid(self):
        self._request_webpage(f'{self._WEBPAGE_HOST}ttwid/register', None, data=json.dumps({
            'aid': self._AID,
            'service': 'www.douyin.com',
        }).encode(), headers={'content-type': 'application/json'})
        cookie = self._get_cookies(self._WEBPAGE_HOST).get('ttwid')
        return cookie.value if cookie else None

    def check_and_get_required_cookies(self):
        for name in self._REQUIRED_COOKIES:
            if self._get_cookies(self._WEBPAGE_HOST).get(name):
                continue
            value = self._get_ttwid() if name == 'ttwid' else self._CSRF
            if not value:
                raise ExtractorError('Unable to generate required cookies')
            self._set_cookie('.douyin.com', name, value)

    def _get_uifid(self):
        names = ('uifid', 'uifid_temp', 'uifidtemp', 'UIFID', 'UIFID_TEMP', 'UIFIDTEMP')
        uifid = traverse_obj(self._get_cookies(self._WEBPAGE_HOST), (names, 'value'), get_all=False)
        if not uifid:
            return self._request_webpage(self._WEBPAGE_HOST, None).headers.get('x-tt-trace-host')
        return uifid

    def _sign_query(self, query):
        uifid = self._get_uifid()
        timestamp = str(int(time.time()))
        query = {**query, 'uifid': uifid, 'timestamp': timestamp}
        encoded_query = urllib.parse.urlencode(query)
        signature = hashlib.md5(f'{uifid}_{timestamp}_{self._SEC_SALT}_{encoded_query}'.encode()).hexdigest()
        return {**query, 'x-secsdk-web-signature': signature}

    def _real_extract(self, url):
        video_id = self._match_id(url)
        self.check_and_get_required_cookies()
        detail = traverse_obj(self._download_json(
            'https://www.douyin.com/aweme/v1/web/aweme/detail/', video_id,
            'Downloading web detail JSON', 'Failed to download web detail JSON',
            query=self._sign_query({'aweme_id': video_id}), fatal=False), ('aweme_detail', {dict}))
        if not detail:
            raise ExtractorError(
                'Fresh cookies (not necessarily logged in) are needed',
                expected=not self._get_cookies(self._WEBPAGE_HOST).get('s_v_web_id'))
        return self._parse_aweme_video_app(detail)
