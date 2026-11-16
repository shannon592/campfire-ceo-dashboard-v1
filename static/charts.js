/* Small Chart.js wrapper so every chart shares one look:
   2px lines, <=24px bars with 4px rounded data-ends, hairline solid grid,
   recessive axes, a legend only when there are 2+ series, hover tooltips. */
(function () {
  "use strict";

  function css(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function fmtMoney(v) {
    if (v === null || v === undefined) return "–";
    var s = v < 0 ? "-" : "";
    var a = Math.abs(v);
    if (a >= 1e6) return s + "$" + (a / 1e6).toFixed(a >= 1e7 ? 0 : 1) + "M";
    if (a >= 1e3) return s + "$" + (a / 1e3).toFixed(0) + "K";
    return s + "$" + a.toFixed(0);
  }
  function fmtMoneyFull(v) {
    if (v === null || v === undefined) return "–";
    return (v < 0 ? "-$" : "$") + Math.abs(v).toLocaleString("en-US", { maximumFractionDigits: 0 });
  }
  function fmtPct(v) {
    return v === null || v === undefined ? "–" : (v * 100).toFixed(1) + "%";
  }

  var FORMATS = {
    money: { axis: fmtMoney, tip: fmtMoneyFull },
    pct: { axis: function (v) { return (v * 100).toFixed(0) + "%"; }, tip: fmtPct },
  };

  function monthLabel(m) {
    var parts = m.split("-");
    var d = new Date(Date.UTC(+parts[0], +parts[1] - 1, 1));
    return d.toLocaleString("en-US", { month: "short", timeZone: "UTC" }) + " " + parts[0].slice(2);
  }

  function baseOptions(fmt, seriesCount) {
    var muted = css("--text-muted");
    var grid = css("--grid");
    var axis = css("--axis");
    return {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: "index", intersect: false },
      layout: { padding: { top: 4, right: 4 } },
      plugins: {
        legend: {
          display: seriesCount > 1,
          position: "top",
          align: "start",
          labels: { color: css("--text-secondary"), boxWidth: 12, boxHeight: 12, usePointStyle: true, pointStyle: "rectRounded" },
        },
        tooltip: {
          backgroundColor: css("--surface"),
          titleColor: css("--text-primary"),
          bodyColor: css("--text-secondary"),
          borderColor: css("--border"),
          borderWidth: 1,
          padding: 10,
          boxPadding: 4,
          usePointStyle: true,
          callbacks: {
            label: function (ctx) {
              var name = ctx.dataset.label ? ctx.dataset.label + ": " : "";
              return " " + name + fmt.tip(ctx.parsed.y);
            },
          },
        },
      },
      scales: {
        x: {
          grid: { display: false },
          border: { color: axis },
          ticks: { color: muted, maxRotation: 0, autoSkipPadding: 12 },
        },
        y: {
          grid: { color: grid, lineWidth: 1 },
          border: { display: false },
          ticks: { color: muted, callback: fmt.axis, maxTicksLimit: 6 },
        },
      },
    };
  }

  function make(id, config) {
    var el = document.getElementById(id);
    if (!el || !window.Chart) return null;
    return new window.Chart(el, config);
  }

  /* series: [{label, data, color: "--series-1", fill: bool}] */
  function line(id, labels, series, opts) {
    opts = opts || {};
    var fmt = FORMATS[opts.format || "money"];
    var options = baseOptions(fmt, series.length);
    if (opts.beginAtZero) options.scales.y.beginAtZero = true;
    return make(id, {
      type: "line",
      data: {
        labels: labels.map(monthLabel),
        datasets: series.map(function (s) {
          var c = css(s.color);
          return {
            label: s.label,
            data: s.data,
            borderColor: c,
            backgroundColor: s.fill ? c + "1a" : c,
            fill: s.fill ? "origin" : false,
            borderWidth: 2,
            borderJoinStyle: "round",
            borderCapStyle: "round",
            tension: 0.25,
            pointRadius: 0,
            pointHoverRadius: 5,
            pointHoverBorderWidth: 2,
            pointHoverBorderColor: css("--surface"),
            pointBackgroundColor: c,
            spanGaps: true,
          };
        }),
      },
      options: options,
    });
  }

  /* Bars. If opts.diverging, positive values use --positive and negatives --negative. */
  function bar(id, labels, series, opts) {
    opts = opts || {};
    var fmt = FORMATS[opts.format || "money"];
    var options = baseOptions(fmt, series.length);
    options.scales.y.beginAtZero = true;
    if (opts.diverging) {
      options.plugins.tooltip.usePointStyle = false;
      options.plugins.tooltip.displayColors = false;
    }
    var pos = css("--positive"), neg = css("--negative");
    return make(id, {
      type: "bar",
      data: {
        labels: opts.categoryLabels ? labels : labels.map(monthLabel),
        datasets: series.map(function (s) {
          var c = css(s.color || "--series-1");
          return {
            label: s.label,
            data: s.data,
            backgroundColor: opts.diverging
              ? s.data.map(function (v) { return v < 0 ? neg : pos; })
              : c,
            borderRadius: 4,
            borderSkipped: "start",
            maxBarThickness: 24,
            categoryPercentage: 0.7,
            barPercentage: 0.9,
          };
        }),
      },
      options: options,
    });
  }

  window.Dash = { line: line, bar: bar, fmtMoney: fmtMoney };
})();
