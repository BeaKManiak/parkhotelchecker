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

  function parseChart(canvas) {
    var raw = canvas.getAttribute("data-chart");
    if (!raw) return null;
    try {
      return JSON.parse(raw);
    } catch (err) {
      console.error("could not parse chart data", err);
      return null;
    }
  }

  function money(value) {
    return "€" + value.toLocaleString(undefined, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2
    });
  }

  // Arrival dates are calendar days, not instants: build the timestamp in the
  // browser's timezone so "13 Feb" stays 13 Feb wherever it is read.
  function localDateMs(iso) {
    var parts = iso.split("-").map(Number);
    return new Date(parts[0], parts[1] - 1, parts[2]).getTime();
  }

  function tickCallback(mode, min, max) {
    var span = max - min;
    var sameYear =
      new Date(min).getFullYear() === new Date(max).getFullYear();
    return function (value) {
      var moment = new Date(value);
      if (mode === "snapshot") {
        return moment.toLocaleDateString(
          undefined,
          sameYear
            ? { day: "2-digit", month: "short" }
            : { day: "2-digit", month: "short", year: "numeric" }
        );
      }
      if (span <= 2 * 86400000) {
        return moment.toLocaleString(undefined, {
          day: "2-digit",
          month: "short",
          hour: "2-digit",
          minute: "2-digit"
        });
      }
      if (!sameYear || span > 365 * 86400000) {
        return moment.toLocaleDateString(undefined, {
          month: "short",
          year: "numeric"
        });
      }
      return moment.toLocaleDateString(undefined, {
        day: "2-digit",
        month: "short"
      });
    };
  }

  function draw(canvas, data, mode) {
    if (typeof window.Chart === "undefined") return null;

    var snapshot = mode === "snapshot";
    var datasets = [];
    var xs = [];

    if (snapshot) {
      var points = data.snapshot.map(function (point) {
        return { x: localDateMs(point.date), y: point.price };
      });
      xs = points.map(function (point) {
        return point.x;
      });
      datasets = [
        {
          label: "Current price",
          data: points,
          borderColor: PALETTE[0],
          backgroundColor: PALETTE[0],
          borderWidth: 2,
          pointRadius: 3,
          pointHoverRadius: 5,
          stepped: false,
          tension: 0,
          spanGaps: false
        }
      ];
    } else {
      datasets = data.time.map(function (item, index) {
        var colour = PALETTE[index % PALETTE.length];
        var points = item.points.map(function (point) {
          return { x: point[0], y: point[1] };
        });
        xs = xs.concat(
          points.map(function (point) {
            return point.x;
          })
        );
        return {
          label: item.label,
          data: points,
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
    }

    if (!xs.length) return null;
    var min = Infinity;
    var max = -Infinity;
    xs.forEach(function (x) {
      if (x < min) min = x;
      if (x > max) max = x;
    });

    return new window.Chart(canvas, {
      type: "line",
      data: { datasets: datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: snapshot
          ? { mode: "nearest", axis: "x", intersect: false }
          : { mode: "index", axis: "x", intersect: false },
        scales: {
          x: {
            type: "linear",
            title: {
              display: true,
              text: snapshot ? "Check-in date" : "Price checked on"
            },
            ticks: {
              maxTicksLimit: 8,
              callback: tickCallback(mode, min, max)
            },
            grid: { display: false }
          },
          y: {
            title: { display: true, text: "Package price" },
            ticks: {
              callback: function (value) {
                return money(value);
              }
            }
          }
        },
        plugins: {
          legend: {
            display: !snapshot,
            labels: { boxWidth: 12, usePointStyle: true }
          },
          tooltip: {
            callbacks: {
              title: function (items) {
                if (!items.length) return "";
                var moment = new Date(items[0].parsed.x);
                return snapshot
                  ? moment.toLocaleDateString(undefined, {
                      weekday: "short",
                      day: "2-digit",
                      month: "short",
                      year: "numeric"
                    })
                  : moment.toLocaleString();
              },
              label: function (item) {
                return snapshot
                  ? money(item.parsed.y)
                  : item.dataset.label + ": " + money(item.parsed.y);
              }
            }
          }
        }
      }
    });
  }

  function setupChart() {
    var canvas = document.getElementById("price-chart");
    if (!canvas) return;

    var data = parseChart(canvas);
    if (!data) return;

    var hint = document.getElementById("chart-hint");
    var buttons = Array.prototype.slice.call(
      document.querySelectorAll("[data-chart-mode]")
    );
    var chart = null;

    function caption(mode) {
      if (!hint) return;
      if (mode === "snapshot") {
        hint.textContent =
          "Latest price for each check-in date" +
          (data.as_of
            ? ", as of " + new Date(data.as_of).toLocaleString()
            : "") +
          ".";
      } else if (data.checks < 2) {
        hint.textContent =
          "Only one check so far. A trend appears after the next check; " +
          "flat segments mean no change was recorded.";
      } else {
        hint.textContent =
          "One stepped line per check-in date; x is when we checked. " +
          "Lines at the same price overlap exactly.";
      }
    }

    function show(mode) {
      if (chart) {
        chart.destroy();
        chart = null;
      }
      chart = draw(canvas, data, mode);
      buttons.forEach(function (button) {
        button.setAttribute(
          "aria-pressed",
          button.getAttribute("data-chart-mode") === mode ? "true" : "false"
        );
      });
      caption(mode);
    }

    buttons.forEach(function (button) {
      var mode = button.getAttribute("data-chart-mode");
      button.disabled =
        mode === "snapshot"
          ? data.snapshot.length === 0
          : data.time.length === 0;
      button.addEventListener("click", function () {
        show(mode);
      });
    });

    var initial = data.default || "snapshot";
    if (initial === "snapshot" && !data.snapshot.length) initial = "time";
    if (initial === "time" && !data.time.length) initial = "snapshot";
    show(initial);
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
    setupChart();
  });
})();
