// LedgerGuard client behaviour. Kept in one static file so a strict Content-Security-Policy can be used.
(function () {
  "use strict";

  // Submit the enclosing form when a filter select changes.
  document.querySelectorAll("[data-autosubmit]").forEach(function (el) {
    el.addEventListener("change", function () { el.form.submit(); });
  });

  // Running debit/credit totals for journal line tables.
  var lines = document.getElementById("lines");
  if (lines && document.getElementById("tdr")) {
    var sum = function (sel) {
      var total = 0;
      lines.querySelectorAll(sel).forEach(function (i) {
        var v = parseFloat(i.value);
        if (!isNaN(v)) { total += Math.round(v * 100); }
      });
      return total;
    };
    var fmt = function (cents) { return (cents / 100).toLocaleString(undefined, { minimumFractionDigits: 2 }); };
    var update = function () {
      var d = sum('input[name$="-debit"]'), c = sum('input[name$="-credit"]');
      document.getElementById("tdr").textContent = fmt(d);
      document.getElementById("tcr").textContent = fmt(c);
      var el = document.getElementById("tdiff");
      el.textContent = d === c ? "Balanced" : "Difference " + fmt(d - c);
      el.className = d === c ? "badge ok" : "badge bad";
    };
    lines.addEventListener("input", update);
    update();
  }

  // Reconciliation manual match: tick items to fill the ID fields.
  var left = document.getElementById("mm-left"), right = document.getElementById("mm-right");
  if (left && right) {
    var ids = function (side) {
      return Array.prototype.map.call(document.querySelectorAll(".pick-" + side + ":checked"), function (c) {
        return c.value;
      }).join(",");
    };
    document.querySelectorAll(".pick-left, .pick-right").forEach(function (cb) {
      cb.addEventListener("change", function () { left.value = ids("left"); right.value = ids("right"); });
    });
  }
})();
