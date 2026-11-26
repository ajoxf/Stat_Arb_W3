"""THE ECONOMIC CALENDAR: the high-impact US releases, and their figures
the moment the agency publishes them.

Official sources only - nothing is scraped from a calendar site:

- **BLS** (Bureau of Labor Statistics): CPI, the jobs report (payrolls,
  the unemployment rate, hourly earnings) and PPI. WHEN comes from
  BLS's own release calendar (`bls.ics`); the FIGURES from the BLS
  public API. A free BLS key (`BLS_API_KEY` in .env) allows 500 reads a
  day; without one the keyless API allows 25, so it is read less often.
- **DOL** (Department of Labor): initial jobless claims, every Thursday
  08:30 New York time (Wednesday in a holiday week). The figure is read
  from DOL's own news release for that day. No key.
- **EIA** (Energy Information Administration): US commercial crude
  inventories, every Wednesday 10:30 New York time (later in a holiday
  week). From the EIA API v2; a free key (`EIA_API_KEY` in .env) is
  required.

A RELEASE is found by its data changing, not by the clock: from its
scheduled time each source is read every few seconds until a figure for
a NEW period appears (or, for DOL, until that day's release is up). A
release that comes late is still caught; one moved by a holiday is
caught when it lands.

No agency publishes forecasts: ForexFactory's weekly feed gives the
consensus, and its schedule of every high-impact G10 event; the surprise
is actual minus that forecast.

At each release the desk's spreads are noted, and again five minutes
later: over time, which releases actually move THIS desk's spreads.

A reading only. Nothing here sizes, filters or sends an order.
"""

import calendar as _calendar
import datetime
import json
import logging
import os
import re
import threading
import time

BLS_ICS_URL = 'https://www.bls.gov/schedule/news_release/bls.ics'
BLS_API_V2 = 'https://api.bls.gov/publicAPI/v2/timeseries/data/'
BLS_API_V1 = 'https://api.bls.gov/publicAPI/v1/timeseries/data/'
DOL_RELEASE_URL = 'https://www.dol.gov/newsroom/releases/eta/eta{date}'
EIA_URL = ('https://api.eia.gov/v2/petroleum/stoc/wstk/data/'
           '?api_key={key}&frequency=weekly&data[0]=value'
           '&facets[series][]=WCESTUS1&sort[0][column]=period'
           '&sort[0][direction]=desc&offset=0&length=3')

#: The releases, each with the figures it carries. `how`: what the
#: headline is - the change in percent over 1 or 12 months, the change
#: in level, or the level itself.
RELEASES = {
    'cpi': {'name': 'CPI', 'agency': 'BLS', 'ics': 'Consumer Price Index',
            'figures': [
                {'key': 'cpi_mm', 'name': 'CPI m/m',
                 'series': 'CUSR0000SA0', 'how': 'pct1', 'unit': '%'},
                {'key': 'core_cpi_mm', 'name': 'Core CPI m/m',
                 'series': 'CUSR0000SA0L1E', 'how': 'pct1', 'unit': '%'},
                {'key': 'cpi_yy', 'name': 'CPI y/y',
                 'series': 'CUUR0000SA0', 'how': 'pct12', 'unit': '%'}]},
    'jobs': {'name': 'Jobs report', 'agency': 'BLS',
             'ics': 'Employment Situation',
             'figures': [
                 {'key': 'nfp', 'name': 'Non-farm payrolls',
                  'series': 'CES0000000001', 'how': 'change', 'unit': 'K'},
                 {'key': 'unemployment', 'name': 'Unemployment rate',
                  'series': 'LNS14000000', 'how': 'level', 'unit': '%'},
                 {'key': 'ahe_mm', 'name': 'Average hourly earnings m/m',
                  'series': 'CES0500000003', 'how': 'pct1', 'unit': '%'}]},
    'ppi': {'name': 'PPI', 'agency': 'BLS', 'ics': 'Producer Price Index',
            'figures': [
                {'key': 'ppi_mm', 'name': 'PPI m/m',
                 'series': 'WPSFD4', 'how': 'pct1', 'unit': '%'},
                {'key': 'core_ppi_mm', 'name': 'Core PPI m/m',
                 'series': 'WPSFD49104', 'how': 'pct1', 'unit': '%'}]},
    'claims': {'name': 'Jobless claims', 'agency': 'DOL',
               'figures': [{'key': 'claims', 'name': 'Initial jobless claims',
                            'unit': 'K'}]},
    'crude': {'name': 'Crude oil inventories', 'agency': 'EIA',
              'figures': [{'key': 'crude', 'name': 'Crude oil inventories',
                           'unit': 'M'}]},
}

#: FOREXFACTORY's weekly calendar, as JSON: every event of the week with
#: its impact, the consensus FORECAST and the previous figure - what no
#: agency publishes. Read once an hour (the feed asks not to be read
#: more often); the official agencies still give the actual the moment
#: it lands.
FF_URL = 'https://nfs.faireconomy.media/ff_calendar_thisweek.json'
FF_EVERY_SEC = 3600.0

#: ForexFactory's names for the figures this desk reads from the
#: agencies (US events only).
FF_FIGURES = {
    'CPI m/m': 'cpi_mm', 'Core CPI m/m': 'core_cpi_mm', 'CPI y/y': 'cpi_yy',
    'Non-Farm Employment Change': 'nfp', 'Unemployment Rate':
    'unemployment', 'Average Hourly Earnings m/m': 'ahe_mm',
    'PPI m/m': 'ppi_mm', 'Core PPI m/m': 'core_ppi_mm',
    'Unemployment Claims': 'claims', 'Crude Oil Inventories': 'crude'}

#: A currency as the screen says it.
WHERE = {'USD': 'US', 'EUR': 'EU', 'GBP': 'UK', 'JPY': 'JP', 'CNY': 'CN',
         'CAD': 'CA', 'AUD': 'AU', 'NZD': 'NZ', 'CHF': 'CH', 'SEK': 'SE',
         'NOK': 'NO'}

#: The ten currencies whose 3-star events the calendar shows by default.
G10 = ('USD', 'EUR', 'JPY', 'GBP', 'CHF', 'CAD', 'AUD', 'NZD', 'SEK', 'NOK')


def parse_ff(rows):
    """ForexFactory's weekly feed: its HIGH-impact events, [{'title',
    'country', 'at' (epoch), 'forecast', 'previous', 'actual'}]. A blank
    figure is None. Events it cannot place in time are left out."""
    out = []
    for r in rows or ():
        if str(r.get('impact') or '').lower() != 'high':
            continue
        try:
            at = datetime.datetime.fromisoformat(
                str(r.get('date')).replace('Z', '+00:00')).timestamp()
        except (TypeError, ValueError):
            continue

        def figure(name):
            value = str(r.get(name) or '').strip()
            return value or None
        out.append({'title': str(r.get('title') or '').strip(),
                    'country': str(r.get('country') or '').upper(),
                    'at': at, 'forecast': figure('forecast'),
                    'previous': figure('previous'),
                    'actual': figure('actual')})
    return out


def unit_of(text):
    """'%', 'K', 'M' or '' - the unit a calendar figure is written in."""
    found = re.search(r'([%KkMmBb])\s*$', str(text or ''))
    return found.group(1).upper() if found else ''


#: How long after its scheduled time a release is looked for, fast and
#: then slowly: an agency that is late is still caught.
FAST_SEC = 15 * 60
LOOK_SEC = 2 * 86400


# -- New York time, without a time-zone database ---------------------------

def _nth_sunday(year, month, n):
    first = datetime.date(year, month, 1)
    day = 1 + (6 - first.weekday()) % 7          # the first Sunday
    return datetime.date(year, month, day + 7 * (n - 1))


def ny_offset_hours(day):
    """New York's offset from UTC on a calendar day: -4 in daylight
    time (second Sunday of March to first Sunday of November), else -5.
    Releases are in the morning, well clear of the 2 am switch."""
    start = _nth_sunday(day.year, 3, 2)
    end = _nth_sunday(day.year, 11, 1)
    return -4 if start <= day < end else -5


def ny_to_utc(day, hour, minute=0):
    """Epoch seconds of `hour:minute` New York time on `day`."""
    local = datetime.datetime(day.year, day.month, day.day, hour, minute,
                              tzinfo=datetime.timezone.utc)
    return local.timestamp() - ny_offset_hours(day) * 3600.0


def ny_day(at):
    """The New York calendar day of an epoch."""
    utc = datetime.datetime.fromtimestamp(at, datetime.timezone.utc)
    guess = utc.date()
    return (utc + datetime.timedelta(hours=ny_offset_hours(guess))).date()


def _fourth_thursday(year):
    first = datetime.date(year, 11, 1)
    day = 1 + (3 - first.weekday()) % 7
    return datetime.date(year, 11, day + 21)


#: Federal holidays that fall on a fixed date and move a Thursday
#: release to the Wednesday.
_FIXED_HOLIDAYS = ((1, 1), (6, 19), (7, 4), (11, 11), (12, 25))


def claims_day(week_thursday):
    """DOL publishes on Thursday - on Wednesday when that Thursday is a
    federal holiday (Thanksgiving, Christmas, New Year...)."""
    t = week_thursday
    if t == _fourth_thursday(t.year) or (t.month, t.day) in _FIXED_HOLIDAYS:
        return t - datetime.timedelta(days=1)
    return t


def crude_slot(week_wednesday):
    """EIA publishes on Wednesday 10:30; when that Wednesday is a federal
    holiday, on Thursday 12:00. After a Monday holiday it can slip by a
    day too - the release is caught when it lands either way."""
    w = week_wednesday
    if (w.month, w.day) in _FIXED_HOLIDAYS:
        return w + datetime.timedelta(days=1), 12, 0
    return w, 10, 30


def weekly_schedule(now, days_back=3, days_ahead=8):
    """The DOL and EIA releases from `days_back` ago to `days_ahead` on:
    [(release key, epoch)]."""
    today = ny_day(now)
    out = []
    for offset in range(-days_back - 7, days_ahead + 7):
        day = today + datetime.timedelta(days=offset)
        if day.weekday() == 3:                          # Thursday
            when = claims_day(day)
            out.append(('claims', ny_to_utc(when, 8, 30)))
        if day.weekday() == 2:                          # Wednesday
            when, hour, minute = crude_slot(day)
            out.append(('crude', ny_to_utc(when, hour, minute)))
    lo, hi = now - days_back * 86400, now + days_ahead * 86400
    return [(k, at) for k, at in out if lo <= at <= hi]


# -- BLS's own calendar ----------------------------------------------------

def parse_ics(text):
    """BLS's calendar file: [(summary, epoch)] for each timed event.
    Times written with a TZID are New York time; with a Z, UTC."""
    out = []
    # Unfold folded lines (a continuation starts with a space).
    text = re.sub(r'\r?\n[ \t]', '', text or '')
    for block in text.split('BEGIN:VEVENT')[1:]:
        summary = re.search(r'^SUMMARY[^:]*:(.*)$', block, re.M)
        start = re.search(r'^DTSTART([^:]*):(\d{8})T(\d{2})(\d{2})(\d{2})?'
                          r'(Z?)', block, re.M)
        if not summary or not start:
            continue
        day = datetime.datetime.strptime(start.group(2), '%Y%m%d').date()
        hour, minute = int(start.group(3)), int(start.group(4))
        if start.group(6) == 'Z':
            at = datetime.datetime(day.year, day.month, day.day, hour,
                                   minute, tzinfo=datetime.timezone.utc
                                   ).timestamp()
        else:
            at = ny_to_utc(day, hour, minute)
        out.append((summary.group(1).strip().replace('\\,', ','), at))
    return out


def bls_schedule(events):
    """The BLS releases this desk follows, from the calendar's events:
    [(release key, epoch)]."""
    out = []
    for summary, at in events or ():
        for key, release in RELEASES.items():
            name = release.get('ics')
            # 'Consumer Price Index', not 'Real Earnings' or the
            # 'Consumer Price Index - Average Price Data' extras.
            if name and summary.startswith(name) and 'Average Price' \
                    not in summary:
                out.append((key, at))
    return out


# -- the figures -----------------------------------------------------------

def _period_key(row):
    """'2026-M10' sorts by time; M13 (annual averages) is not a month."""
    period = row.get('period') or ''
    if not re.fullmatch(r'M(0[1-9]|1[0-2])', period):
        return None
    return f'{row.get("year")}-{period}'


def bls_series(body):
    """{series id: [(period, value)], newest first} from a BLS answer."""
    if (body or {}).get('status') not in (None, 'REQUEST_SUCCEEDED'):
        raise ValueError('; '.join(body.get('message') or [])
                         or body.get('status'))
    out = {}
    for series in ((body or {}).get('Results') or {}).get('series') or ():
        rows = []
        for row in series.get('data') or ():
            key = _period_key(row)
            try:
                value = float(str(row.get('value')).replace(',', ''))
            except (TypeError, ValueError):
                continue
            if key:
                rows.append((key, value))
        rows.sort(reverse=True)
        out[series.get('seriesID')] = rows
    return out


def _headline(rows, how, at=0):
    """One figure from a series' months, newest first, read as of the
    month at index `at` (0 the latest, 1 the one before it)."""
    try:
        now = rows[at][1]
        if how == 'level':
            return round(now, 1)
        if how == 'change':
            return round(now - rows[at + 1][1], 0)
        if how == 'pct1':
            return round((now / rows[at + 1][1] - 1.0) * 100.0, 1)
        if how == 'pct12':
            year_ago = dict(rows).get(_year_back(rows[at][0]))
            if year_ago is None:
                return None
            return round((now / year_ago - 1.0) * 100.0, 1)
    except (IndexError, ZeroDivisionError):
        return None
    return None


def _year_back(period):
    year, month = period.split('-')
    return f'{int(year) - 1}-{month}'


def bls_figures(release, series):
    """The release's figures from the series: {'period' (the latest
    month), figure key: (actual, previous)}."""
    out = {'period': None}
    for figure in RELEASES[release]['figures']:
        rows = series.get(figure['series']) or []
        if not rows:
            continue
        out['period'] = max(out['period'] or '', rows[0][0])
        out[figure['key']] = (_headline(rows, figure['how'], 0),
                              _headline(rows, figure['how'], 1))
    return out


_NUM = r'(\d{1,3}(?:,\d{3})+|\d+)'


def dol_claims(html):
    """Initial jobless claims from DOL's news release: (actual, previous)
    - the seasonally adjusted headline, and last week's level as revised
    in the same release. None when the page has no such figure."""
    text = re.sub(r'<[^>]+>', ' ', html or '')
    text = re.sub(r'\s+', ' ', text)
    found = re.search(r'seasonally adjusted initial claims was ' + _NUM,
                      text, re.I)
    if not found:
        return None
    actual = int(found.group(1).replace(',', ''))
    previous = None
    revised = re.search(r"previous week's level was revised (?:up|down) "
                        r'by ' + _NUM + ' from ' + _NUM + ' to ' + _NUM,
                        text, re.I)
    if revised:
        previous = int(revised.group(3).replace(',', ''))
    else:
        same = re.search(r"previous week's (?:level was )?unrevised "
                         r'(?:level of |at )?' + _NUM, text, re.I)
        moved = re.search(r'(increase|decrease) of ' + _NUM +
                          r" from the previous week's", text, re.I)
        if same:
            previous = int(same.group(1).replace(',', ''))
        elif moved:
            step = int(moved.group(2).replace(',', ''))
            previous = actual - step if moved.group(1).lower() == \
                'increase' else actual + step
    return actual, previous


def eia_crude(body):
    """Crude inventories from the EIA answer: (week, change, previous
    change), in thousand barrels. The headline is the WEEK's change."""
    rows = ((body or {}).get('response') or {}).get('data') or []
    rows = sorted(((r.get('period'), float(r.get('value')))
                   for r in rows if r.get('value') is not None),
                  reverse=True)
    if len(rows) < 2:
        return None
    change = rows[0][1] - rows[1][1]
    previous = rows[1][1] - rows[2][1] if len(rows) > 2 else None
    return rows[0][0], change, previous


# -- written the way a calendar writes it -----------------------------------

def show(value, unit):
    """`0.3%`, `216K`, `-2.1M`, `+254K` - a figure as a calendar writes
    it. Claims in thousands of people; crude in millions of barrels
    (EIA gives thousands); payrolls in thousands of jobs."""
    if value is None:
        return None
    if unit == '%':
        return f'{value:.1f}%'
    if unit == 'M':
        return f'{value / 1000.0:.1f}M'
    if unit == 'K':
        return f'{value:,.0f}K'
    return f'{value:g}'


def number(text):
    """A typed figure as a number in the figure's own unit: '0.3%' 0.3,
    '221K' 221, '-0.6M' -0.6, '1,250K' 1250. None if not a number."""
    if text is None:
        return None
    raw = str(text).strip().replace(',', '').replace('−', '-')
    found = re.fullmatch(r'([+-]?\d+(?:\.\d+)?)\s*([%KkMmBb]?)', raw)
    if not found:
        return None
    return float(found.group(1))


def surprise_words(actual, forecast, unit):
    """'0.1 pt above forecast', '12K below forecast', 'as forecast'."""
    a, f = number(actual), number(forecast)
    if a is None or f is None:
        return None
    gap = round(a - f, 3)
    if abs(gap) < 1e-9:
        return 'as forecast'
    size = abs(gap)
    amount = (f'{size:.1f} pt' if unit == '%' else
              f'{size:.1f}M' if unit == 'M' else
              f'{size:,.0f}K' if unit == 'K' else f'{size:g}')
    return f'{amount} {"above" if gap > 0 else "below"} forecast'


# -- the calendar ----------------------------------------------------------

def _utc_day(at):
    return datetime.datetime.fromtimestamp(at, datetime.timezone.utc).date()


def _ff_id(row):
    slug = re.sub(r'[^a-z0-9]+', '-', row['title'].lower()).strip('-')
    return f"ff:{row['country'].lower()}:{slug}:{_utc_day(row['at'])}"


def _http_get(url, timeout=12.0):
    import requests
    response = requests.get(url, timeout=timeout, headers={
        'User-Agent': 'Mozilla/5.0 (economic calendar; desk)',
        'Accept': '*/*'})
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.text


def _http_post_json(url, body, timeout=12.0):
    import requests
    response = requests.post(url, json=body, timeout=timeout)
    response.raise_for_status()
    return response.json()


class Calendar:
    """The week's releases and their figures, read in the background.

    `spreads()` gives {pair key: {'name', 'mid'}} - the desk's spreads
    now - so each release can note what they did. `env()` gives the
    .env values (re-read each time, so a key saved from the screen is
    used without a restart).
    """

    def __init__(self, state_path, forecasts_path=None, spreads=None,
                 currencies=G10,
                 env=None, clock=time.time, get=None, post=None,
                 background=True, schedule_every_sec=6 * 3600.0):
        self.state_path = state_path
        self.forecasts_path = forecasts_path
        self.spreads = spreads or (lambda: {})
        self.env = env or (lambda: dict(os.environ))
        self.clock = clock
        self.get = get
        self.post = post
        self.background = background
        self.schedule_every_sec = schedule_every_sec
        self.currencies = tuple(c.strip().upper() for c in currencies
                                if c and c.strip())
        self._lock = threading.Lock()
        self._busy = False
        self._next_read = 0.0
        self.errors = {}
        self.state = self._load()

    # -- the saved state -----------------------------------------------------

    def _load(self):
        try:
            with open(self.state_path, encoding='utf-8') as f:
                state = json.load(f)
        except (OSError, ValueError):
            state = {}
        state.setdefault('events', {})
        state.setdefault('ics', [])
        state.setdefault('ics_at', 0)
        state.setdefault('latest', {})
        state.setdefault('ff', [])
        state.setdefault('ff_at', 0)
        state.setdefault('ff_out', {})       # id -> when its actual showed
        return state

    def _save(self):
        tmp = self.state_path + '.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self.state, f)
            os.replace(tmp, self.state_path)
        except OSError as e:
            logging.info('calendar: could not save: %s', e)

    # -- the schedule --------------------------------------------------------

    def _fetch(self, url):
        return (self.get or _http_get)(url)

    def _refresh_schedule(self, now):
        if now - (self.state.get('ics_at') or 0) >= self.schedule_every_sec:
            try:
                text = self._fetch(BLS_ICS_URL)
                if text:
                    self.state['ics'] = [list(e) for e in parse_ics(text)]
                    self.state['ics_at'] = now
                    self.errors.pop('BLS calendar', None)
            except Exception as e:
                self.errors['BLS calendar'] = f'not read: {e}'[:160]
                # Try again in an hour, not on every pass.
                self.state['ics_at'] = now - self.schedule_every_sec + 3600
        rows = weekly_schedule(now) + [
            (k, at) for k, at in bls_schedule(self.state.get('ics'))
            if now - 3 * 86400 <= at <= now + 8 * 86400]
        events = self.state['events']
        for key, at in rows:
            event_id = f'{key}:{_utc_day(at):%Y-%m-%d}'
            if event_id not in events:
                events[event_id] = {'id': event_id, 'release': key,
                                    'at': at, 'status': 'upcoming'}
            elif events[event_id]['status'] == 'upcoming':
                events[event_id]['at'] = at
        # Keep a fortnight's history; drop the rest.
        for event_id in [i for i, e in events.items()
                         if e['at'] < now - 14 * 86400]:
            del events[event_id]

    # -- reading the figures -------------------------------------------------

    def _bls(self, release):
        keys = self.env()
        key = (keys.get('BLS_API_KEY') or '').strip()
        series = [f['series'] for f in RELEASES[release]['figures']]
        year = _utc_day(self.clock()).year
        body = {'seriesid': series, 'startyear': str(year - 2),
                'endyear': str(year)}
        if key:
            body['registrationkey'] = key
        answer = (self.post or _http_post_json)(
            BLS_API_V2 if key else BLS_API_V1, body)
        return bls_figures(release, bls_series(answer))

    def _eia(self):
        key = (self.env().get('EIA_API_KEY') or '').strip()
        if not key:
            raise LookupError('no EIA key: add EIA_API_KEY (free, '
                              'eia.gov/opendata) in the Calendar window')
        text = self._fetch(EIA_URL.format(key=key))
        found = eia_crude(json.loads(text) if text else {})
        if not found:
            return {'period': None}
        week, change, previous = found
        return {'period': week, 'crude': (change, previous)}

    def _dol(self, at):
        day = ny_day(at)
        text = self._fetch(DOL_RELEASE_URL.format(date=f'{day:%Y%m%d}'))
        found = dol_claims(text) if text else None
        if text and not found and len(text) > 2000:
            # A page that IS there but does not read: said, never left
            # to look like a release that has not come out.
            raise ValueError(f'DOL release of {day:%d %b} read, but its '
                             f'claims figure was not found in it')
        if found:                       # people -> thousands, as 'K'
            found = (found[0] / 1000.0, None if found[1] is None
                     else found[1] / 1000.0)
        return {'period': f'{day:%Y-%m-%d}' if found else None,
                'claims': found}

    def _read(self, release, at):
        agency = RELEASES[release]['agency']
        if agency == 'BLS':
            return self._bls(release)
        if agency == 'EIA':
            return self._eia()
        return self._dol(at)

    def _interval(self, release, since):
        """How often a release that is due is read: every 10 s for the
        first quarter hour (60 s on the keyless BLS API, which allows 25
        reads a day), then every 2 minutes, then every 15."""
        keyless = RELEASES[release]['agency'] == 'BLS' and not \
            (self.env().get('BLS_API_KEY') or '').strip()
        if since < FAST_SEC:
            return 60.0 if keyless else 10.0
        if since < 3600:
            return 300.0 if keyless else 120.0
        return 900.0

    def _baseline(self, release, now, at):
        """The latest figures BEFORE a release: what 'previous' shows,
        and the period a new release must be newer than. Read at most
        every six hours per release. For DOL, last week's release."""
        known = self.state['latest'].get(release)
        if known and now - known.get('at', 0) < 6 * 3600:
            return known
        try:
            if RELEASES[release]['agency'] == 'DOL':
                day = ny_day(at)
                thursday = day + datetime.timedelta(days=(3 - day.weekday())
                                                    % 7)
                last = claims_day(thursday - datetime.timedelta(days=7))
                figures = self._dol(ny_to_utc(last, 8, 30))
                figures['period'] = None   # a DOL release is its own day
            else:
                figures = self._read(release, now)
            known = {'at': now, 'period': figures.get('period'),
                     'figures': {k: v for k, v in figures.items()
                                 if k != 'period'}}
            self.state['latest'][release] = known
            self.errors.pop(RELEASES[release]['agency'], None)
        except Exception as e:
            self.errors[RELEASES[release]['agency']] = str(e)[:160]
            known = known or {'at': now, 'period': None, 'figures': {}}
            known['at'] = now - 6 * 3600 + 600      # again in 10 minutes
            self.state['latest'][release] = known
        return known

    def _note_spreads(self, event, field):
        try:
            spreads = self.spreads() or {}
        except Exception:
            spreads = {}
        moves = event.setdefault('spreads', {})
        for key, row in spreads.items():
            if row.get('mid') is None:
                continue
            entry = moves.setdefault(key, {'name': row.get('name') or key})
            entry[field] = row['mid']
            if field == 'after' and entry.get('at') is not None:
                entry['move'] = round(entry['after'] - entry['at'], 4)

    def _refresh_ff(self, now):
        """ForexFactory's week, once an hour (every 15 minutes while
        an event of the day is waiting for its actual): the schedule,
        the forecasts and the previous figures."""
        pending = any(
            e['at'] <= now and not e.get('actual') and
            now - e['at'] < 3 * 3600 for e in self.state.get('ff') or ())
        every = 900.0 if pending else FF_EVERY_SEC
        if now - (self.state.get('ff_at') or 0) < every:
            return
        self.state['ff_at'] = now
        try:
            text = self._fetch(FF_URL)
            rows = parse_ff(json.loads(text) if text else [])
        except Exception as e:
            self.errors['ForexFactory'] = f'not read: {e}'[:160]
            return
        if rows:
            self.state['ff'] = rows
            self.errors.pop('ForexFactory', None)
        for row in rows:
            if row.get('actual'):
                self.state['ff_out'].setdefault(_ff_id(row), now)

    def step(self):
        """One pass: the schedule, then every release that is due."""
        now = self.clock()
        self._refresh_ff(now)
        self._refresh_schedule(now)
        for event in sorted(self.state['events'].values(),
                            key=lambda e: e['at']):
            release = event['release']
            since = now - event['at']
            if event['status'] == 'upcoming':
                if since < -3600:
                    continue
                # An hour ahead: last period's figures as 'previous'.
                base = self._baseline(release, now, event['at'])
                event['previous'] = {
                    k: (v[0] if isinstance(v, (list, tuple)) else v)
                    for k, v in (base.get('figures') or {}).items()}
                event['base_period'] = base.get('period')
                if since >= 0:
                    event['status'] = 'due'
                    self._note_spreads(event, 'at')
                    event['next_read'] = now
            if event['status'] == 'released' and not event.get('after_done') \
                    and since >= 300:
                self._note_spreads(event, 'after')
                event['after_done'] = True
            if event['status'] != 'due':
                continue
            if since > LOOK_SEC:
                event['status'] = 'missed'
                continue
            if now < event.get('next_read', 0):
                continue
            event['next_read'] = now + self._interval(release, since)
            try:
                figures = self._read(release, event['at'])
                self.errors.pop(RELEASES[release]['agency'], None)
            except Exception as e:
                self.errors[RELEASES[release]['agency']] = str(e)[:160]
                continue
            period = figures.get('period')
            if not period or (event.get('base_period')
                              and period <= event['base_period']):
                continue                            # not out yet
            event['status'] = 'released'
            event['released_at'] = now
            event['period'] = period
            event['figures'] = {k: list(v) for k, v in figures.items()
                                if k != 'period' and v is not None}
            self.state['latest'][release] = {
                'at': now, 'period': period,
                'figures': event['figures']}
            if since >= 300:                 # caught late: no 5-min move
                event['after_done'] = True
        self._save()

    def refresh_if_due(self):
        """Run a pass on a thread of its own when one is due; never
        waits for an agency."""
        now = self.clock()
        with self._lock:
            if self._busy or now < self._next_read:
                return False
            self._busy = True
            self._next_read = now + 5.0
        if self.background:
            threading.Thread(target=self._run, daemon=True,
                             name='econ-calendar').start()
        else:
            self._run()
        return True

    def _run(self):
        try:
            self.step()
        except Exception as e:                     # a reading, never a fault
            logging.warning('calendar: %s', e)
        finally:
            with self._lock:
                self._busy = False

    # -- what the screen and Telegram read -------------------------------

    def snapshot(self):
        now = self.clock()
        keys = self.env()
        rows = []
        ff_rows = [r for r in self.state.get('ff') or ()
                   if not self.currencies or r['country'] in self.currencies]
        ff_usd = [r for r in ff_rows if r['country'] == 'USD'
                  and r['title'] in FF_FIGURES]

        def ff_match(key, at):
            # The same release on ForexFactory: the same figure within two
            # days (a holiday moves one; the two sources need not agree on
            # the day).
            near = [r for r in ff_usd if FF_FIGURES[r['title']] == key
                    and abs(r['at'] - at) <= 2 * 86400]
            return min(near, key=lambda r: abs(r['at'] - at)) \
                if near else None
        matched = set()
        for event in sorted(self.state['events'].values(),
                            key=lambda e: e['at']):
            release = RELEASES.get(event['release'])
            if not release:
                continue
            figures = []
            via_ff = []
            own_ff = []
            for figure in release['figures']:
                key = figure['key']
                got = (event.get('figures') or {}).get(key)
                actual = show(got[0], figure['unit']) if got else None
                previous = (show(got[1], figure['unit']) if got and
                            got[1] is not None else
                            show((event.get('previous') or {}).get(key),
                                 figure['unit']))
                ff = ff_match(key, event['at'])
                if actual is None and ff and ff.get('actual'):
                    # The agency not read (blocked, late, no key): the
                    # figure as ForexFactory has it, said as such.
                    actual = ff['actual']
                    via_ff.append(key)
                forecast = (ff or {}).get('forecast')
                if previous is None and ff:
                    previous = ff.get('previous')
                if ff:
                    matched.add(_ff_id(ff))
                    own_ff.append(_ff_id(ff))
                figures.append({
                    'key': key, 'name': figure['name'],
                    'unit': figure['unit'], 'actual': actual,
                    'previous': previous, 'forecast': forecast,
                    'surprise': surprise_words(actual, forecast,
                                               figure['unit'])})
            status, released_at = event['status'], event.get('released_at')
            # ForexFactory's time for a release not yet out: the tighter
            # schedule (a holiday, a moved release).
            at = event['at']
            ff_times = [r['at'] for r in ff_usd if _ff_id(r) in own_ff]
            if status == 'upcoming' and ff_times:
                at = min(ff_times)
            if status in ('due', 'missed') and via_ff and \
                    all(f['actual'] for f in figures):
                status = 'released'
                released_at = min(
                    (self.state['ff_out'].get(i) for i in own_ff
                     if self.state['ff_out'].get(i)), default=now)
            rows.append({
                'id': event['id'], 'release': event['release'],
                'via': 'ForexFactory' if via_ff else None,
                'name': release['name'], 'agency': release['agency'],
                'country': 'USD', 'where': 'US', 'impact': 3,
                'at': at, 'source': release['agency'],
                'status': status, 'released_at': released_at,
                'period': event.get('period'),
                'figures': figures,
                'spreads': event.get('spreads') or {}})
        # Every other high-impact event of the week, from ForexFactory:
        # its time, forecast and previous, and its actual once the feed
        # has it.
        for ff in ff_rows:
            event_id = _ff_id(ff)
            if event_id in matched:
                continue
            seen = self.state['ff_out'].get(event_id)
            actual = ff.get('actual')
            unit = unit_of(actual or ff.get('forecast') or ff.get('previous'))
            forecast = ff.get('forecast')
            rows.append({
                'id': event_id, 'release': 'ff', 'name': ff['title'],
                'agency': 'ForexFactory', 'source': 'ForexFactory',
                'country': ff['country'],
                'where': WHERE.get(ff['country'], ff['country']),
                'impact': 3, 'at': ff['at'],
                'status': ('released' if actual else 'upcoming'
                           if ff['at'] > now else 'past'),
                'released_at': (seen or ff['at']) if actual else None,
                'period': None, 'spreads': {},
                'figures': [{'key': 'ff', 'name': ff['title'], 'unit': unit,
                             'actual': actual,
                             'previous': ff.get('previous'),
                             'forecast': forecast,
                             'surprise': surprise_words(actual, forecast,
                                                        unit)}]})
        rows.sort(key=lambda r: r['at'])
        # NEXT: what is still to come - a release waiting for its figures
        # only for its first half hour, never as stale news a day later.
        upcoming = [r for r in rows if r['status'] == 'upcoming'
                    or (r['status'] == 'due' and now - r['at'] < 1800)]
        released = [r for r in rows if r['status'] == 'released']
        sources = {
            'BLS': ('key saved' if (keys.get('BLS_API_KEY') or '').strip()
                    else 'no key: 25 reads a day, read every minute'),
            'DOL': 'no key needed',
            'EIA': ('key saved' if (keys.get('EIA_API_KEY') or '').strip()
                    else 'no key: crude inventories are not read'),
            'ForexFactory': ('schedule and forecasts, read hourly'
                             if self.state.get('ff') else 'not read yet')}
        return {'events': rows,
                'next': upcoming[0] if upcoming else None,
                'last': (max(released, key=lambda r: r['released_at']
                             or 0) if released else None),
                'sources': sources, 'errors': dict(self.errors),
                'now': now}
