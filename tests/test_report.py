"""The printable trading report, from the Analysis record."""

from okxtrader import analysis, report


def _trip(i, pnl, side='BUY', source='ALGO'):
    opened = 1_700_000_000 + i * 3600
    return {'position_id': f'p{i}', 'pair_key': 'ethbtc', 'side': side,
            'quantity': 2.0, 'source': source, 'opened_at': opened,
            'closed_at': opened + 1800, 'entry_spread': -20.0,
            'exit_spread': -20.0 + pnl / 0.2, 'spread_units': 0.2,
            'realized_pnl': pnl, 'entry_margin': 10.0,
            'close_reason': 'Algo: take profit'}


def test_the_report_has_the_returns_the_curve_and_every_trade():
    rows = [_trip(0, 1.0), _trip(1, -0.5, source='MANUAL'), _trip(2, 2.0)]
    body = analysis.report(rows, now=1_700_100_000, capital=1000)
    page = report.render(body, {'ethbtc': 'ETH vs BTC perps'}, 7, None,
                         1_700_100_000, logo=report.NEXUS_MARK)
    assert '+$2.50' in page                        # net
    assert '+0.25%' in page                        # on $1,000 capital
    assert '66.7%' in page                         # 2 won, 1 lost
    assert '<svg class="equity"' in page
    assert page.count('ETH vs BTC perps</td>') == 3 + 1   # journal + split
    assert 'Manual' in page and 'Algo' in page
    # Oldest first, with the running total.
    journal = page[page.index('<table class="journal">'):]
    assert journal.index('+$1.00</b>') < journal.index('−$0.50</b>')


def test_an_empty_period_says_so():
    body = analysis.report([], now=1_700_100_000)
    page = report.render(body, {}, 1, None, 1_700_100_000)
    assert 'No closed trade in this period.' in page
    assert 'capital not set' in page


def test_money_never_prints_minus_zero():
    assert report.money(-0.001) == '$0.00'
    assert report.money(-1234.5) == '−$1,234.50'


def test_net_pnl_can_be_masked_or_left_out():
    rows = [_trip(0, 1.0), _trip(1, -0.5, source='MANUAL'), _trip(2, 2.0)]
    body = analysis.report(rows, now=1_700_100_000, capital=1000)

    def page(mode):
        return report.render(body, {'ethbtc': 'ETH vs BTC perps'}, 7, None,
                             1_700_100_000, pnl=mode,
                             link=lambda m: f'/report?pnl={m}')
    shown, masked, hidden = page('show'), page('mask'), page('hide')
    assert '+$2.50' in shown and report.MASK not in shown
    # Masked: the columns stay, the dollars are dots.
    assert '+$2.50' not in masked and '+$1.00' not in masked
    assert report.MASK in masked and '<th>Net P&amp;L</th>' in masked
    # Hidden: the money columns are left out; the percentages stay.
    assert '<th>Net P&amp;L</th>' not in hidden
    assert '<th>Running</th>' not in hidden
    assert '+$' not in hidden and report.MASK not in hidden
    assert '+0.25%' in hidden and '66.7%' in hidden
    # The toolbar switches between the three.
    assert 'href="/report?pnl=mask"' in shown
    assert 'class="on" href="/report?pnl=hide"' in hidden


def test_a_masked_report_does_not_give_the_capital_away():
    """Capital x the % on capital would put the dollars back."""
    body = analysis.report([_trip(0, 1.0)], now=1_700_100_000, capital=1000)
    for mode in ('mask', 'hide'):
        page = report.render(body, {}, 7, None, 1_700_100_000, pnl=mode)
        assert '$1,000.00' not in page, mode
    page = report.render(body, {}, 7, None, 1_700_100_000)
    assert '$1,000.00' in page


# -- the client's report -------------------------------------------------------

def _client_body(capital=1000):
    rows = [_trip(0, 1.0), _trip(1, -0.5, side='SELL', source='MANUAL'),
            _trip(30, 2.0)]
    return analysis.report(rows, now=1_700_200_000, capital=capital)


def _text(page):
    """What a reader sees: the page without its style sheet or tags."""
    import re
    page = re.sub(r'<style>.*?</style>', ' ', page, flags=re.S)
    return re.sub(r'<[^>]+>', ' ', page)


TRADE_DETAILS = ('ETH vs BTC perps', 'ethbtc', 'Short', 'Long', 'Manual', 'Algo',
                 'take profit', 'Trade journal', 'Breakdown', 'Win rate',
                 'Trades closed', 'margin', '-20.00')


def test_the_client_report_says_nothing_of_what_was_traded():
    page = report.render_client(_client_body(), 7, 1_700_200_000,
                                logo=report.NEXUS_MARK)
    for word in TRADE_DETAILS:
        assert word not in _text(page), word
    assert '+0.25%' in page and '+$2.50' in page     # the returns stay
    assert 'Returns by day' in page


def test_CONTROL_the_trader_report_says_all_of_it():
    page = report.render(_client_body(), {'ethbtc': 'ETH vs BTC perps'}, 7, None,
                         1_700_200_000, logo=report.NEXUS_MARK)
    for word in ('ETH vs BTC perps', 'Short', 'Manual', 'Algo',
                 'Trade journal', 'Win rate', 'margin', '-20.00'):
        assert word in _text(page), word


def test_the_client_report_sums_by_day_never_by_trade():
    """Two trades closed on one day are ONE row: the count is not given
    away by the table."""
    page = report.render_client(_client_body(), 7, 1_700_200_000)
    body = page[page.index('<tbody>'):page.index('</tbody>')]
    assert body.count('<tr>') == 2                  # 3 trades, 2 days
    assert '+$0.50' in body and '+$2.00' in body    # 1.0 - 0.5 ; 2.0
    assert '+0.05%' in body and '+0.25%' in body    # cumulative on $1,000


def test_a_long_period_is_summed_by_month():
    page = report.render_client(_client_body(), 0, 1_700_200_000)
    assert 'Returns by month' in page and '<th class="l">Month</th>' in page
    # Control: a week is by day.
    assert 'Returns by day' in report.render_client(_client_body(), 7,
                                                    1_700_200_000)


def test_the_client_report_masks_and_hides_the_money():
    body = _client_body()
    masked = report.render_client(body, 7, 1_700_200_000, pnl='mask')
    hidden = report.render_client(body, 7, 1_700_200_000, pnl='hide')
    for page in (masked, hidden):
        assert '+$' not in page and '$1,000.00' not in page
        assert '+0.25%' in page                     # % on capital stays
    assert report.MASK in masked and '<th>Net P&amp;L</th>' in masked
    assert '<th>Net P&amp;L</th>' not in hidden
    shown = report.render_client(body, 7, 1_700_200_000)
    assert '+$2.50' in shown and '$1,000.00' in shown          # control


def test_without_capital_the_client_report_says_so():
    page = report.render_client(_client_body(capital=0), 7, 1_700_200_000)
    assert 'capital not set' in page and 'not set' in page


# -- fills, fees, notional value and the worst / best points ----------------


def _full(units=34.107, n=12):
    """A realistic period: maker / taker fills and fees from OKX, the
    extremes of every trade, the account balance as the capital."""
    rows, fills, deal = [], [], 0
    for i in range(n):
        opened = 1_700_000_000 + i * 7200
        tickets = (i * 10 + 1, i * 10 + 2)
        for tk in tickets:
            for end in ('open', 'close'):
                deal += 1
                kind = 'taker' if deal % 4 == 0 else 'maker'
                fills.append({'deal_id': str(deal), 'account': 'okx',
                              'position_ticket': str(tk), 'entry': end,
                              'order_type': kind,
                              'fee': -3.4 if kind == 'maker' else -8.5})
        move = -5.0 if i % 5 == 4 else 2.0
        rows.append({
            'position_id': f'p{i}', 'pair_key': 'ethbtc',
            'side': 'BUY' if i % 2 else 'SELL', 'quantity': 341.07,
            'source': 'ALGO', 'opened_at': opened, 'closed_at': opened + 3600,
            'entry_spread': -1.2, 'exit_spread': -1.2 + move,
            'spread_units': units, 'realized_pnl': move * units - 20.0,
            'entry_margin': 4.9844,
            'close_reason': 'Algo: exit at the mean (below the target)',
            'peak_pnl': 3.0 * units, 'trough_pnl': -2.5 * units,
            'leg_a': {'side': 'SELL', 'volume': 19.5, 'contract_size': 0.01,
                      'price': 87420.5, 'position_tickets': [tickets[0]]},
            'leg_b': {'side': 'BUY', 'volume': 58, 'contract_size': 0.1,
                      'price': 2925.4, 'position_tickets': [tickets[1]]}})
    body = analysis.report(rows, now=1_700_100_000, capital=3744)
    body['summary']['capital_source'] = 'balance'
    body['fee_tally'] = analysis.attach_fills(body['journal'], rows, fills)
    return body


def test_the_report_carries_the_fills_the_fees_and_the_notional():
    page = report.render(_full(), {'ethbtc': 'ETH vs BTC'}, 7, None,
                         1_700_100_000)
    assert 'Fills A/B<br>in → out' in page and 'M/M → M/T' in page
    assert 'Notional value' in page and '$34,014' in page
    assert 'Exposure' not in page
    assert '<b>Fees</b>' in page and '% of fills maker' in page
    assert '$3,744.00 (account balance)' in page


def test_the_report_reads_the_worst_and_best_points_on_capital_and_margin():
    page = report.render(_full(), {'ethbtc': 'ETH vs BTC'}, 7, None,
                         1_700_100_000)
    section = page[page.index('Worst and best points'):]
    for head in ('MAE<br>% capital', 'MAE<br>% margin', 'MFE<br>% capital',
                 'MFE<br>% margin'):
        assert head in section, head
    # 2.5 x 34.107 = 85.27 down: of $3,744 and of 341.07 x 4.9844.
    assert '−2.28%' in section and '−5.02%' in section
    assert '+2.73%' in section and '+6.02%' in section


def test_hidden_p_and_l_keeps_the_percentages_and_drops_the_money():
    page = report.render(_full(), {'ethbtc': 'ETH vs BTC'}, 7, None,
                         1_700_100_000, pnl='hide')
    section = page[page.index('Worst and best points'):]
    assert '<th>MAE</th>' not in section and '−2.28%' in section
    assert 'Notional value' not in page and '$34,014' not in page
    assert '<p class="tally"><b>Fills</b>' in page
    assert '<p class="tally"><b>Fees</b>' not in page


def test_nothing_in_the_printed_report_runs_outside_its_column(tmp_path):
    """A4 landscape, every mode, at today's size and at a thousand times
    it: no cell, tile or table wider than the room it has."""
    import pytest
    from test_ui_browser import chromium_path
    sync_playwright = pytest.importorskip(
        'playwright.sync_api').sync_playwright
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(executable_path=chromium_path())
        except Exception as e:
            pytest.skip(f'chromium unavailable: {e}')
        page = browser.new_page(viewport={'width': 1060, 'height': 1400})
        page.emulate_media(media='print')
        for units in (34.107, 34107.0):
            body = _full(units)
            for mode in report.PNL_MODES:
                html = report.render(body, {'ethbtc': 'ETH vs BTC'}, 7, None,
                                     1_700_100_000, pnl=mode,
                                     link=lambda m: '#')
                page.set_content(html)
                bad = page.evaluate("""() => {
                  const out = [];
                  document.querySelectorAll(
                      'td, th, .tv, .tn, .tl, .kv div, .meta div')
                    .forEach(c => { if (c.scrollWidth > c.clientWidth + 1)
                      out.push(c.textContent.slice(0, 40)); });
                  const m = document.querySelector('main')
                    .getBoundingClientRect();
                  document.querySelectorAll('table').forEach(t => {
                    if (t.getBoundingClientRect().right > m.right + 1)
                      out.push('a table wider than the page'); });
                  return out;
                }""")
                assert bad == [], (units, mode, bad)
        browser.close()
