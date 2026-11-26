"""The economic calendar: official US releases, found by their data."""

import datetime
import json

from okxtrader import econcal as ec


def utc(y, m, d, hh, mm):
    return datetime.datetime(y, m, d, hh, mm,
                             tzinfo=datetime.timezone.utc).timestamp()


# -- New York time -----------------------------------------------------------

def test_new_york_time_follows_daylight_saving():
    # 08:30 New York: 12:30 UTC in summer, 13:30 UTC in winter.
    assert ec.ny_to_utc(datetime.date(2026, 7, 9), 8, 30) == \
        utc(2026, 7, 9, 12, 30)
    assert ec.ny_to_utc(datetime.date(2026, 11, 25), 8, 30) == \
        utc(2026, 11, 25, 13, 30)
    # The switches: second Sunday of March, first Sunday of November.
    assert ec.ny_offset_hours(datetime.date(2026, 3, 7)) == -5
    assert ec.ny_offset_hours(datetime.date(2026, 3, 8)) == -4
    assert ec.ny_offset_hours(datetime.date(2026, 10, 31)) == -4
    assert ec.ny_offset_hours(datetime.date(2026, 11, 1)) == -5


def test_claims_come_a_day_early_in_a_holiday_week():
    # Thanksgiving 2026 is Thursday 26 November: claims on Wednesday 25th.
    assert ec.claims_day(datetime.date(2026, 11, 26)) == \
        datetime.date(2026, 11, 25)
    assert ec.claims_day(datetime.date(2026, 12, 3)) == \
        datetime.date(2026, 12, 3)
    # Christmas Day 2025 was a Thursday.
    assert ec.claims_day(datetime.date(2025, 12, 25)) == \
        datetime.date(2025, 12, 24)


def test_crude_moves_to_thursday_noon_when_wednesday_is_a_holiday():
    # Veterans Day 2026 is Wednesday 11 November.
    assert ec.crude_slot(datetime.date(2026, 11, 11)) == \
        (datetime.date(2026, 11, 12), 12, 0)
    assert ec.crude_slot(datetime.date(2026, 11, 25)) == \
        (datetime.date(2026, 11, 25), 10, 30)


def test_the_week_has_its_claims_and_its_crude():
    now = utc(2026, 11, 25, 9, 0)                    # Wed morning, UTC
    rows = ec.weekly_schedule(now, days_back=0, days_ahead=9)
    assert ('claims', utc(2026, 11, 25, 13, 30)) in rows
    assert ('crude', utc(2026, 11, 25, 15, 30)) in rows
    assert ('claims', utc(2026, 12, 3, 13, 30)) in rows
    assert ('crude', utc(2026, 12, 2, 15, 30)) in rows


# -- BLS's calendar ------------------------------------------------------------

ICS = """BEGIN:VCALENDAR\r
BEGIN:VEVENT\r
DTSTART;TZID=US-Eastern:20261210T083000\r
SUMMARY:Consumer Price Index\r
END:VEVENT\r
BEGIN:VEVENT\r
DTSTART;TZID=US-Eastern:20261204T083000\r
SUMMARY:Employment Situation\r
END:VEVENT\r
BEGIN:VEVENT\r
DTSTART:20261211T133000Z\r
SUMMARY:Producer Price Index\r
END:VEVENT\r
BEGIN:VEVENT\r
DTSTART;TZID=US-Eastern:20261210T083000\r
SUMMARY:Real Earnings\r
END:VEVENT\r
BEGIN:VEVENT\r
DTSTART;TZID=US-Eastern:20261210T100000\r
SUMMARY:Consumer Price Index - Average Price Dat\r
 a\r
END:VEVENT\r
END:VCALENDAR\r
"""


def test_the_bls_calendar_gives_cpi_jobs_and_ppi_and_nothing_else():
    rows = ec.bls_schedule(ec.parse_ics(ICS))
    assert rows == [('cpi', utc(2026, 12, 10, 13, 30)),
                    ('jobs', utc(2026, 12, 4, 13, 30)),
                    ('ppi', utc(2026, 12, 11, 13, 30))]


# -- the figures ----------------------------------------------------------------

def bls_answer(series):
    """A BLS API answer: {series id: [(year, 'M10', value)], newest
    first}."""
    return {'status': 'REQUEST_SUCCEEDED', 'Results': {'series': [
        {'seriesID': sid, 'data': [
            {'year': str(y), 'period': p, 'value': str(v)}
            for y, p, v in rows]}
        for sid, rows in series.items()]}}


def months(values, year=2026, month=10):
    """[(year, 'Mnn', value)], newest first, back from year-month."""
    out = []
    for v in values:
        out.append((year, f'M{month:02d}', v))
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return out


CPI = {'CUSR0000SA0': months([324.0, 323.03, 322.1]),
       'CUSR0000SA0L1E': months([330.0, 329.01, 328.3]),
       'CUUR0000SA0': months([324.5, 324.1] + [320.0] * 10 + [315.2, 315.0])}


def test_cpi_is_read_as_bls_states_it():
    figures = ec.bls_figures('cpi', ec.bls_series(bls_answer(CPI)))
    assert figures['period'] == '2026-M10'
    assert figures['cpi_mm'] == (0.3, 0.3)
    assert figures['core_cpi_mm'] == (0.3, 0.2)
    # 324.5 over October 2025's 315.2.
    assert figures['cpi_yy'] == (3.0, 2.9)


def test_payrolls_are_the_change_and_unemployment_the_level():
    answer = bls_answer({'CES0000000001': months([159500, 159246, 159100]),
                         'LNS14000000': months([4.2, 4.1, 4.1]),
                         'CES0500000003': months([36.30, 36.19, 36.05])})
    figures = ec.bls_figures('jobs', ec.bls_series(answer))
    assert figures['nfp'] == (254, 146)
    assert figures['unemployment'] == (4.2, 4.1)
    assert ec.show(figures['nfp'][0], 'K') == '254K'
    assert ec.show(figures['unemployment'][0], '%') == '4.2%'


def test_a_refused_bls_read_says_why():
    try:
        ec.bls_series({'status': 'REQUEST_NOT_PROCESSED',
                       'message': ['daily threshold reached']})
    except ValueError as e:
        assert 'threshold' in str(e)
    else:
        raise AssertionError('a refusal must not read as no data')


DOL = """<html><body><p>In the week ending November 21, the advance figure
for <b>seasonally adjusted initial claims</b> was 209,000, a decrease of
13,000 from the previous week's revised level. The previous week's level
was revised up by 1,000 from 221,000 to 222,000.</p></body></html>"""


def test_claims_are_read_from_dols_release():
    # 'seasonally adjusted initial claims</b> was' - tags are dropped.
    assert ec.dol_claims(DOL) == (209000, 222000)
    unrevised = DOL.replace("The previous week's level\nwas revised up by "
                            "1,000 from 221,000 to 222,000.", '')
    assert ec.dol_claims(unrevised) == (209000, 222000)   # 209 + 13
    assert ec.dol_claims('<html>Page not found</html>') is None


def test_crude_is_the_weeks_change_in_millions():
    body = {'response': {'data': [
        {'period': '2026-11-20', 'value': 421500},
        {'period': '2026-11-13', 'value': 423600},
        {'period': '2026-11-06', 'value': 422100}]}}
    assert ec.eia_crude(body) == ('2026-11-20', -2100.0, 1500.0)
    assert ec.show(-2100.0, 'M') == '-2.1M'


def test_a_typed_forecast_gives_the_surprise():
    assert ec.surprise_words('0.4%', '0.3%', '%') == '0.1 pt above forecast'
    assert ec.surprise_words('209K', '221K', 'K') == '12K below forecast'
    assert ec.surprise_words('-2.1M', '-0.6M', 'M') == '1.5M below forecast'
    assert ec.surprise_words('2.8%', '2.8%', '%') == 'as forecast'
    assert ec.surprise_words('2.8%', '', '%') is None
    assert ec.surprise_words('2.8%', 'soon', '%') is None


# -- a release, found by its data --------------------------------------------

class Agencies:
    """BLS, DOL and EIA, as the calendar sees them."""

    def __init__(self):
        self.bls = bls_answer(CPI)
        self.pages = {'bls.ics': ICS}
        self.posts = []

    def get(self, url):
        for key, page in self.pages.items():
            if key in url:
                return page
        return None                                  # 404: not out yet

    def post(self, url, body):
        self.posts.append((url, body))
        return self.bls


class Clock:
    def __init__(self, at):
        self.at = at

    def __call__(self):
        return self.at


def calendar(tmp_path, clock, agencies, spreads=None, env=None):
    return ec.Calendar(str(tmp_path / 'cal.json'),
                       str(tmp_path / 'forecasts.json'),
                       spreads=spreads, env=lambda: dict(env or {}),
                       clock=clock, get=agencies.get, post=agencies.post,
                       background=False)


def test_without_an_eia_key_crude_says_so(tmp_path):
    clock = Clock(utc(2026, 11, 25, 15, 30))
    cal = calendar(tmp_path, clock, Agencies())
    cal.step()
    snap = cal.snapshot()
    assert 'no key' in snap['sources']['EIA']
    assert 'EIA_API_KEY' in snap['errors']['EIA']


def test_keyless_bls_is_read_once_a_minute(tmp_path):
    cal = calendar(tmp_path, Clock(0), Agencies())
    assert cal._interval('cpi', 30) == 60.0
    cal.env = lambda: {'BLS_API_KEY': 'k'}
    assert cal._interval('cpi', 30) == 10.0
    assert cal._interval('claims', 30) == 10.0
    assert cal._interval('claims', 1800) == 120.0


def test_the_state_survives_a_restart(tmp_path):
    clock = Clock(utc(2026, 11, 25, 13, 31))
    agencies = Agencies()
    agencies.pages['eta20261125'] = DOL
    cal = calendar(tmp_path, clock, agencies)
    cal.step()
    clock.at += 15
    cal.step()
    again = calendar(tmp_path, clock, agencies)
    assert again.state['events']['claims:2026-11-25']['status'] == \
        'released'


# -- the engine and the screen -------------------------------------------------

def _app(tmp_path):
    from okxtrader.webapp import create_app
    (tmp_path / 'config.json').write_text('{"accounts": {}, "pairs": {}}')
    return create_app(str(tmp_path / 'status.json'),
                      str(tmp_path / 'c.jsonl'), str(tmp_path / 'r.json'),
                      str(tmp_path / 'config.json'),
                      db_path=str(tmp_path / 'x.db')).test_client()


def test_the_keys_go_to_env_and_are_never_shown(tmp_path, monkeypatch):
    # Set then cleared: the test's own writes are undone afterwards.
    for key in ('EIA_API_KEY', 'BLS_API_KEY'):
        monkeypatch.setenv(key, 'x')
        monkeypatch.delenv(key)
    client = _app(tmp_path)
    assert client.get('/api/calendar/keys').get_json()['keys'] == {
        'BLS_API_KEY': False, 'EIA_API_KEY': False}
    client.post('/api/calendar/keys', json={'EIA_API_KEY': 'abcdef123456'})
    assert 'EIA_API_KEY="abcdef123456"' in (tmp_path / '.env').read_text()
    body = client.get('/api/calendar/keys').get_json()
    assert body['keys']['EIA_API_KEY'] is True
    assert 'abcdef123456' not in json.dumps(body)
    assert client.post('/api/calendar/keys', json={
        'EIA_API_KEY': 'no spaces allowed'}).status_code == 400


def test_a_dol_page_that_does_not_read_is_said_not_waited_on(tmp_path):
    clock = Clock(utc(2026, 11, 25, 13, 30))
    agencies = Agencies()
    agencies.pages['eta20261125'] = '<html>' + 'x' * 5000 + '</html>'
    cal = calendar(tmp_path, clock, agencies)
    cal.step()
    clock.at += 10
    cal.step()
    assert 'claims figure was not found' in cal.snapshot()['errors']['DOL']


# -- ForexFactory: the schedule, the forecasts, the events no agency has ------

FF_WEEK = [
    {'title': 'Unemployment Claims', 'country': 'USD',
     'date': '2026-11-25T08:30:00-05:00', 'impact': 'High',
     'forecast': '221K', 'previous': '222K'},
    {'title': 'FOMC Meeting Minutes', 'country': 'USD',
     'date': '2026-11-25T14:00:00-05:00', 'impact': 'High',
     'forecast': '', 'previous': ''},
    {'title': 'CPI Flash Estimate y/y', 'country': 'EUR',
     'date': '2026-11-27T05:00:00-05:00', 'impact': 'High',
     'forecast': '2.1%', 'previous': '2.0%'},
    {'title': 'German Ifo', 'country': 'EUR',
     'date': '2026-11-24T04:00:00-05:00', 'impact': 'Medium',
     'forecast': '88.0', 'previous': '87.5'},
    {'title': 'BOJ Gov Speaks', 'country': 'JPY',
     'date': '2026-11-26T01:00:00-05:00', 'impact': 'High'},
    {'title': 'Manufacturing PMI', 'country': 'CNY',
     'date': '2026-11-26T20:30:00-05:00', 'impact': 'High'}]


def test_forexfactory_gives_only_the_high_impact_events():
    rows = ec.parse_ff(FF_WEEK)
    assert [r['title'] for r in rows] == [
        'Unemployment Claims', 'FOMC Meeting Minutes',
        'CPI Flash Estimate y/y', 'BOJ Gov Speaks', 'Manufacturing PMI']
    assert rows[0]['at'] == utc(2026, 11, 25, 13, 30)
    assert rows[0]['forecast'] == '221K' and rows[1]['forecast'] is None


def test_a_release_forexfactory_has_on_another_day_is_one_release(tmp_path):
    """The weekly rule says Thursday; ForexFactory says Wednesday (a
    holiday the rule does not know): one row, at ForexFactory's time."""
    clock = Clock(utc(2026, 11, 30, 9, 0))           # Monday
    agencies = Agencies()
    agencies.pages['faireconomy'] = json.dumps([
        {'title': 'Unemployment Claims', 'country': 'USD',
         'date': '2026-12-02T08:30:00-05:00', 'impact': 'High',
         'forecast': '219K', 'previous': '216K'}])
    cal = calendar(tmp_path, clock, agencies)
    cal.step()
    rows = [e for e in cal.snapshot()['events']
            if 'laims' in e['name'] or e['release'] == 'claims']
    assert len(rows) == 1
    [claims] = rows
    assert claims['at'] == utc(2026, 12, 2, 13, 30)
    assert claims['figures'][0]['forecast'] == '219K'


def test_the_account_ledger_is_kept_by_bill_id(tmp_path):
    from okxtrader.database import Store
    store = Store(str(tmp_path / 'x.db'))
    rows = [{'billId': 'b1', 'ts': '1000', 'type': '2', 'instId': 'BTC',
             'balChg': '-0.2', 'bal': '3499.8', 'fee': '-0.2',
             'tradeId': 't1'}]
    assert store.record_bills('okx', rows) == 1
    store.record_bills('okx', rows)                   # again: no double
    [bill] = store.bills()
    assert bill['bal'] == 3499.8 and bill['trade_id'] == 't1'
    assert bill['fee'] == -0.2 and bill['ts_ms'] == 1000



# -- the calendar is ForexFactory's; the agencies fill in US actuals ----------

def ff(title, when, country='USD', **figures):
    return dict({'title': title, 'country': country, 'date': when,
                 'impact': 'High'}, **figures)


def test_cpi_takes_its_actual_from_bls_the_moment_a_new_month_lands(tmp_path):
    clock = Clock(utc(2026, 12, 10, 12, 45))         # 45 min before CPI
    agencies = Agencies()
    agencies.pages['faireconomy'] = json.dumps([
        ff('CPI m/m', '2026-12-10T08:30:00-05:00', forecast='0.3%',
           previous='0.3%'),
        ff('Core CPI m/m', '2026-12-10T08:30:00-05:00', forecast='0.3%',
           previous='0.3%'),
        ff('CPI y/y', '2026-12-10T08:30:00-05:00', forecast='3.0%',
           previous='3.0%')])
    spread = {'mid': -31.10}
    cal = calendar(tmp_path, clock, agencies,
                   spreads=lambda: {'eth': {'name': 'ETH vs BTC',
                                            'mid': spread['mid']}},
                   env={'BLS_API_KEY': 'k'})
    cal.step()
    rows = {e['name']: e for e in cal.snapshot()['events']}
    assert set(rows) == {'CPI m/m', 'Core CPI m/m', 'CPI y/y'}
    assert rows['CPI m/m']['status'] == 'upcoming'
    assert rows['CPI m/m']['figures'][0]['forecast'] == '0.3%'
    # 08:30 New York: BLS has not moved yet - nothing made up.
    clock.at = utc(2026, 12, 10, 13, 30)
    cal.step()
    assert cal.state['events']['cpi:2026-12-10']['status'] == 'due'
    assert all(not e['figures'][0]['actual']
               for e in cal.snapshot()['events'])
    agencies.bls = bls_answer({
        'CUSR0000SA0': months([325.3] + [324.0, 323.03], month=11),
        'CUSR0000SA0L1E': months([331.0, 330.0, 329.01], month=11),
        'CUUR0000SA0': months([325.0, 324.5] + [320.0] * 10 + [315.2],
                              month=11)})
    clock.at += 12
    spread['mid'] = -30.40
    cal.step()
    snap = cal.snapshot()
    rows = {e['name']: e for e in snap['events']}
    assert [rows[n]['figures'][0]['actual']
            for n in ('CPI m/m', 'Core CPI m/m', 'CPI y/y')] == \
        ['0.4%', '0.3%', '3.1%']
    assert rows['CPI m/m']['status'] == 'released'
    assert rows['CPI m/m']['source'] == 'ForexFactory \u00b7 actual BLS'
    assert rows['CPI m/m']['figures'][0]['surprise'] == \
        '0.1 pt above forecast'
    clock.at = utc(2026, 12, 10, 13, 35)
    spread['mid'] = -29.90
    cal.step()
    move = {e['name']: e for e in cal.snapshot()['events']}[
        'CPI m/m']['spreads']['eth']
    assert move['at'] == -31.10 and move['move'] == 1.2
    assert agencies.posts[-1][0] == ec.BLS_API_V2


def test_claims_take_their_actual_from_dol(tmp_path):
    clock = Clock(utc(2026, 11, 25, 13, 0))
    agencies = Agencies()
    agencies.pages['faireconomy'] = json.dumps([
        ff('Unemployment Claims', '2026-11-25T08:30:00-05:00',
           forecast='221K', previous='222K')])
    cal = calendar(tmp_path, clock, agencies)
    cal.step()
    clock.at = utc(2026, 11, 25, 13, 30)
    cal.step()
    agencies.pages['eta20261125'] = DOL
    clock.at += 10
    cal.step()
    [row] = cal.snapshot()['events']
    assert row['status'] == 'released'
    assert row['figures'][0]['actual'] == '209K'
    assert row['figures'][0]['previous'] == '222K'       # ForexFactory's
    assert row['figures'][0]['surprise'] == '12K below forecast'


def test_every_g10_event_is_forexfactorys_over_the_next_two_weeks(tmp_path):
    clock = Clock(utc(2026, 11, 25, 13, 0))
    agencies = Agencies()
    agencies.pages['ff_calendar_thisweek'] = json.dumps(FF_WEEK)
    agencies.pages['ff_calendar_nextweek'] = json.dumps([
        ff('Non-Farm Employment Change', '2026-12-04T08:30:00-05:00',
           forecast='150K', previous='119K'),
        ff('RBA Rate Statement', '2026-12-08T22:30:00-05:00', 'AUD'),
        # Beyond two weeks: not shown.
        ff('FOMC Statement', '2026-12-16T14:00:00-05:00')])
    cal = calendar(tmp_path, clock, agencies)
    cal.step()
    snap = cal.snapshot()
    names = [e['name'] for e in snap['events']]
    for name in ('Unemployment Claims', 'FOMC Meeting Minutes',
                 'CPI Flash Estimate y/y', 'BOJ Gov Speaks',
                 'Non-Farm Employment Change', 'RBA Rate Statement'):
        assert name in names, name
    assert 'FOMC Statement' not in names                  # > 2 weeks
    assert 'Manufacturing PMI' not in names               # CNY: not G10
    assert 'German Ifo' not in names                      # medium impact
    # The agencies add no rows of their own: one claims row, ForexFactory's.
    assert names.count('Unemployment Claims') == 1
    assert len([e for e in snap['events'] if 'laims' in e['name']]) == 1
    [claims] = [e for e in snap['events']
                if e['name'] == 'Unemployment Claims']
    assert claims['figures'][0]['forecast'] == '221K'
    assert snap['next']['name'] == 'Unemployment Claims'


def test_next_week_missing_is_no_fault(tmp_path):
    clock = Clock(utc(2026, 11, 23, 9, 0))
    agencies = Agencies()
    agencies.pages['ff_calendar_thisweek'] = json.dumps(FF_WEEK)
    cal = calendar(tmp_path, clock, agencies)
    cal.step()
    snap = cal.snapshot()
    assert 'ForexFactory' not in snap['errors']
    assert snap['events']


def test_a_past_event_without_its_figures_is_not_next(tmp_path):
    clock = Clock(utc(2026, 11, 25, 13, 0))
    agencies = Agencies()
    agencies.pages['faireconomy'] = json.dumps(FF_WEEK)
    cal = calendar(tmp_path, clock, agencies)
    cal.step()
    assert cal.snapshot()['next']['name'] == 'Unemployment Claims'
    clock.at = utc(2026, 11, 25, 14, 0)                  # claims passed
    cal.step()
    snap = cal.snapshot()
    assert snap['next']['name'] == 'FOMC Meeting Minutes'
    [claims] = [e for e in snap['events']
                if e['name'] == 'Unemployment Claims']
    assert claims['status'] == 'past'


def test_the_engine_hands_its_spreads_over_and_publishes_the_reading(tmp_path):
    from types import SimpleNamespace
    from okxtrader.coordinator import Coordinator
    clock = Clock(utc(2026, 11, 25, 13, 0))
    agencies = Agencies()
    agencies.pages['faireconomy'] = json.dumps(FF_WEEK)
    cal = calendar(tmp_path, clock, agencies)
    desk = SimpleNamespace(calendar=cal, _calendar_spreads={},
                           config=SimpleNamespace(get=lambda k, d=None: d))
    snap = Coordinator._economic_calendar(desk, {
        'eth': {'name': 'ETH vs BTC', 'long_spread': -31.2,
                'short_spread': -31.0},
        'cold': {'name': 'no prices', 'long_spread': None}})
    assert desk._calendar_spreads == {'eth': {'name': 'ETH vs BTC',
                                              'mid': -31.1}}
    assert snap['alert_min'] == 15.0
    assert snap['next']['name'] == 'Unemployment Claims'
    desk.calendar = None
    assert Coordinator._economic_calendar(desk, {}) is None


def test_the_spreads_move_is_noted_over_any_event(tmp_path):
    clock = Clock(utc(2026, 11, 27, 9, 59))
    agencies = Agencies()
    agencies.pages['faireconomy'] = json.dumps([
        ff('CPI Flash Estimate y/y', '2026-11-27T05:00:00-05:00', 'EUR',
           forecast='2.1%', previous='2.0%')])
    spread = {'mid': -20.0}
    cal = calendar(tmp_path, clock, agencies,
                   spreads=lambda: {'eth': {'name': 'ETH vs BTC',
                                            'mid': spread['mid']}})
    cal.step()
    clock.at = utc(2026, 11, 27, 10, 0, )
    cal.step()
    spread['mid'] = -19.25
    clock.at = utc(2026, 11, 27, 10, 6)
    cal.step()
    [row] = cal.snapshot()['events']
    assert row['spreads']['eth']['move'] == 0.75
