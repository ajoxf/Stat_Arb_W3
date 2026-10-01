/* Candlesticks for the spread's band charts — a self-contained Chart.js
 * plugin (no financial-chart add-on to download: a blocked CDN has taken a
 * trading screen down before).
 *
 *   SpreadCandlesChart.config(opts)  → a Chart.js config: candles, EMA, upper
 *                                      and lower band, entry line (optional)
 *   SpreadCandlesChart.set(chart, points, entryZ, entryLevel)
 *                                    → load /api/candles points into it
 *
 * Candles are drawn BEFORE the datasets, so the EMA and the bands sit on top
 * as on TradingView. Up candles green, down candles red; the current (still
 * forming) candle is the last one. Two invisible datasets carry the highs and
 * lows so the y-axis always fits the wicks.
 */
(function () {
  var UP = '#26a69a', DOWN = '#ef5350';

  var plugin = {
    id: 'spreadCandles',
    beforeDatasetsDraw: function (chart) {
      var bars = chart.$candles || [];
      if (!bars.length) { return; }
      var x = chart.scales.x, y = chart.scales.y, ctx = chart.ctx, area = chart.chartArea;
      var step = bars.length > 1 ? Math.abs(x.getPixelForValue(1) - x.getPixelForValue(0))
                                 : area.right - area.left;
      var w = Math.max(1, Math.min(14, step * 0.65));
      ctx.save();
      ctx.beginPath();
      ctx.rect(area.left, area.top, area.right - area.left, area.bottom - area.top);
      ctx.clip();
      for (var i = 0; i < bars.length; i++) {
        var b = bars[i];
        if (!b || b.close == null) { continue; }
        var o = b.open != null ? b.open : b.close, h = b.high != null ? b.high : b.close,
            l = b.low != null ? b.low : b.close, c = b.close;
        var px = x.getPixelForValue(i), col = c >= o ? UP : DOWN;
        ctx.strokeStyle = col; ctx.fillStyle = col; ctx.lineWidth = 1;
        ctx.beginPath();                                     // wick
        ctx.moveTo(Math.round(px) + 0.5, y.getPixelForValue(h));
        ctx.lineTo(Math.round(px) + 0.5, y.getPixelForValue(l));
        ctx.stroke();
        var top = y.getPixelForValue(Math.max(o, c)), bot = y.getPixelForValue(Math.min(o, c));
        ctx.fillRect(px - w / 2, top, w, Math.max(1, bot - top));   // body (≥ 1 px for a doji)
        if (i === bars.length - 1) {                          // the forming candle: outlined
          ctx.strokeStyle = '#333';
          ctx.strokeRect(px - w / 2 - 1.5, top - 1.5, w + 3, Math.max(1, bot - top) + 3);
        }
      }
      ctx.restore();
    }
  };

  function config(opts) {
    opts = opts || {};
    var small = opts.fontSize || 9;
    return {
      type: 'line',
      data: {labels: [], datasets: [
        {label: 'EMA', data: [], borderColor: '#e07b00', borderWidth: 1.3, pointRadius: 0},
        {label: 'Upper', data: [], borderColor: '#1f7ac2', borderWidth: 1, borderDash: [4, 3], pointRadius: 0},
        {label: 'Lower', data: [], borderColor: '#1f7ac2', borderWidth: 1, borderDash: [4, 3], pointRadius: 0},
        {label: 'Entry', data: [], borderColor: '#6a4fa3', borderWidth: 1, borderDash: [2, 2], pointRadius: 0},
        // invisible: keep the wicks inside the y-axis
        {label: '_high', data: [], borderColor: 'rgba(0,0,0,0)', pointRadius: 0},
        {label: '_low', data: [], borderColor: 'rgba(0,0,0,0)', pointRadius: 0}]},
      options: {
        animation: false, responsive: true,
        maintainAspectRatio: opts.maintainAspectRatio !== false,
        interaction: {mode: 'index', intersect: false},
        plugins: {
          legend: {display: true, labels: {boxWidth: 8, font: {size: small},
                   filter: function (it, data) {
                     if (it.text.charAt(0) === '_') { return false; }
                     var d = data.datasets[it.datasetIndex].data || [];
                     return d.some(function (v) { return v != null; });   // no Entry key while flat
                   }}},
          tooltip: {
            filter: function (it) { return it.dataset.label.charAt(0) !== '_' && it.raw != null; },
            callbacks: {
              footer: function (items) {
                var ch = items.length && items[0].chart, b = ch && ch.$candles && ch.$candles[items[0].dataIndex];
                if (!b) { return ''; }
                var f = function (v) { return v == null ? '—' : Number(v).toFixed(2); };
                return 'O ' + f(b.open) + '  H ' + f(b.high) + '  L ' + f(b.low) + '  C ' + f(b.close);
              }
            }
          }
        },
        scales: {x: {ticks: {maxTicksLimit: opts.xTicks || 6, font: {size: small}}},
                 y: {ticks: {font: {size: small}}}}
      },
      plugins: [plugin]
    };
  }

  function set(chart, pts, entryZ, entryLevel, labelFn) {
    if (!chart || !chart.data) { return; }
    var e = Number(entryZ || 2);
    chart.$candles = pts;
    chart.data.labels = pts.map(function (p) { return labelFn ? labelFn(p.t) : p.t; });
    var ds = chart.data.datasets;
    ds[0].data = pts.map(function (p) { return p.mean; });
    ds[1].data = pts.map(function (p) { return p.mean == null ? null : p.mean + e * p.std; });
    ds[2].data = pts.map(function (p) { return p.mean == null ? null : p.mean - e * p.std; });
    ds[3].data = pts.map(function () { return (entryLevel != null && isFinite(entryLevel)) ? entryLevel : null; });
    ds[4].data = pts.map(function (p) { return p.high != null ? p.high : p.close; });
    ds[5].data = pts.map(function (p) { return p.low != null ? p.low : p.close; });
    chart.update('none');
  }

  window.SpreadCandlesChart = {config: config, set: set, plugin: plugin};
})();
