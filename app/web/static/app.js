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

  function setupParty() {
    var party = document.querySelector("[data-party]");
    if (!party) return;

    var adults = party.querySelector("[data-adults]");
    var children = party.querySelector("[data-children]");
    var fields = Array.prototype.slice.call(
      party.querySelectorAll("[data-child-field]")
    );
    var maxParty = parseInt(party.getAttribute("data-max-party"), 10) || 7;

    function syncFields() {
      var count = parseInt(children.value, 10) || 0;
      fields.forEach(function (field, index) {
        var visible = index < count;
        field.hidden = !visible;
        var input = field.querySelector("input");
        if (input) input.disabled = !visible;
      });
    }

    function rebuildChoices() {
      var maxChildren = Math.max(
        0,
        maxParty - (parseInt(adults.value, 10) || 1)
      );
      var selected = Math.min(
        parseInt(children.value, 10) || 0,
        maxChildren
      );
      children.innerHTML = "";
      for (var n = 0; n <= maxChildren; n += 1) {
        var option = document.createElement("option");
        option.value = String(n);
        option.textContent = String(n);
        if (n === selected) option.selected = true;
        children.appendChild(option);
      }
      syncFields();
    }

    adults.addEventListener("change", rebuildChoices);
    children.addEventListener("change", syncFields);
    rebuildChoices();
  }

  function setupCheckAll() {
    function boxes(name) {
      return Array.prototype.slice.call(
        document.querySelectorAll('input[name="' + name + '"]')
      );
    }

    document.querySelectorAll("[data-check-all]").forEach(function (button) {
      button.addEventListener("click", function () {
        boxes(button.getAttribute("data-check-all")).forEach(function (box) {
          box.checked = true;
        });
      });
    });

    document.querySelectorAll("[data-check-none]").forEach(function (button) {
      button.addEventListener("click", function () {
        boxes(button.getAttribute("data-check-none")).forEach(function (box) {
          box.checked = false;
        });
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    setupParty();
    setupCheckAll();
    var canvas = document.getElementById("price-chart");
    if (canvas) draw(canvas);
  });
})();
