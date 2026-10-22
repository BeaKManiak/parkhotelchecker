/* The only JavaScript in the app: draw the price history Chart.js renders.
 *
 * No inline script anywhere, so the CSP can stay at script-src 'self'.
 * Data arrives in a data-* attribute as JSON, which keeps the template free of
 * script tags and the markup free of hand-built serialisation.
 */
(function () {
  "use strict";

  var PALETTE = [
    "#1b4f72", "#c0392b", "#1f6f43", "#8e44ad", "#d35400",
    "#16a085", "#7f8c8d", "#2c3e50", "#a93226", "#27ae60",
    "#2980b9", "#b9770e"
  ];

  function parseData(canvas) {
    var raw = canvas.getAttribute("data-series");
    if (!raw) return [];
    try {
      return JSON.parse(raw);
    } catch (err) {
      console.error("could not parse chart data", err);
      return [];
    }
  }

  function money(value) {
    return "€" + value.toLocaleString(undefined, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2
    });
  }

  function draw(canvas) {
    var series = parseData(canvas);
    if (!series.length || typeof window.Chart === "undefined") return;

    var datasets = series.map(function (item, index) {
      var colour = PALETTE[index % PALETTE.length];
      return {
        label: item.label,
        data: item.points.map(function (point) {
          return { x: point[0], y: point[1] };
        }),
        borderColor: colour,
        backgroundColor: colour,
        borderWidth: 2,
        pointRadius: item.points.length < 3 ? 3 : 0,
        pointHoverRadius: 4,
        stepped: true,
        tension: 0,
        spanGaps: true
      };
    });

    new window.Chart(canvas, {
      type: "line",
      data: { datasets: datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "nearest", axis: "x", intersect: false },
        scales: {
          x: {
            type: "linear",
            ticks: {
              maxTicksLimit: 8,
              callback: function (value) {
                return new Date(value).toLocaleDateString(undefined, {
                  day: "2-digit",
                  month: "short"
                });
              }
            },
            grid: { display: false }
          },
          y: {
            ticks: {
              callback: function (value) {
                return money(value);
              }
            }
          }
        },
        plugins: {
          legend: { labels: { boxWidth: 12, usePointStyle: true } },
          tooltip: {
            callbacks: {
              title: function (items) {
                if (!items.length) return "";
                return new Date(items[0].parsed.x).toLocaleString();
              },
              label: function (item) {
                return item.dataset.label + ": " + money(item.parsed.y);
              }
            }
          }
        }
      }
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    var canvas = document.getElementById("price-chart");
    if (canvas) draw(canvas);
  });
})();
