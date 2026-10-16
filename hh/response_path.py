"""Shared response-route spelling for observation, receipts and browser guards."""
import re
from urllib.parse import unquote

RESPONSE_PATH = '/applicant/vacancy_response'
_WHITESPACE = r'[\x09-\x0d\x1c-\x20\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]'


def canonical_path(path):
    """Conservatively fold routing aliases; never parse a decoded path as a URL."""
    while True:
        decoded = re.sub(_WHITESPACE, '', unquote(path)).replace('\\', '/').lower()
        if decoded == path:
            break
        path = decoded
    segments = []
    for segment in path.split('/'):
        if segment == '..':
            if segments:
                segments.pop()
        elif segment != '.':
            segment = segment.rstrip('.')
            if segment:
                segments.append(segment)
    return '/' + '/'.join(segments)


def is_response_path(path):
    # Preserve the existing conservative response-prefix rule, including
    # response subroutes and unknown suffixes. This is not submit approval.
    return canonical_path(path).startswith(RESPONSE_PATH)


# The same transform in renderer-side guards. Percent runs are decoded with
# replacement, like urllib.unquote; malformed escapes must not hide a route.
CANONICAL_PATH_SCRIPT = r'''path => {
    for (;;) {
        const decoded = path.replace(/(?:%[0-9a-f]{2})+/gi, run =>
            new TextDecoder('utf-8', {ignoreBOM:true}).decode(Uint8Array.from(
                run.match(/%[0-9a-f]{2}/gi), byte => parseInt(byte.slice(1),16))))
            .replace(__WHITESPACE__/g,'').replace(/\\/g,'/').toLowerCase();
        if (decoded === path) break;
        path = decoded;
    }
    const segments = [];
    for (let segment of path.split('/')) {
        if (segment === '..') segments.pop();
        else if (segment !== '.') {
            segment = segment.replace(/\.+$/,'');
            if (segment) segments.push(segment);
        }
    }
    return '/' + segments.join('/');
}'''.replace('__WHITESPACE__', '/' + _WHITESPACE)
