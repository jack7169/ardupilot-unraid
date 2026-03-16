// metrics.js — live system metrics ticker in the navbar
// Polls /autotest/api/metrics every 3s and updates the #sys-metrics elements
(function() {
    function update() {
        fetch('/autotest/api/metrics')
            .then(function(r) { return r.json(); })
            .then(function(d) {
                var cpu = document.getElementById('cpu-pct');
                var mem = document.getElementById('mem-pct');
                var run = document.getElementById('running-count');
                var icon = document.getElementById('metrics-icon');
                if (!cpu) return;

                cpu.textContent = d.cpu_percent.toFixed(0);
                mem.textContent = d.memory_percent.toFixed(0);
                run.textContent = d.running_tests;

                // Icon based on CPU load
                var pct = d.cpu_percent;
                if (pct >= 95) {
                    icon.innerHTML = '<i class="bi bi-fire text-danger metrics-pulse" style="font-size:18px"></i>';
                    icon.title = 'CPU on fire! ' + pct.toFixed(0) + '%';
                } else if (pct >= 80) {
                    icon.innerHTML = '<i class="bi bi-rocket-takeoff text-warning metrics-pulse" style="font-size:18px"></i>';
                    icon.title = 'Heavy load: ' + pct.toFixed(0) + '%';
                } else if (pct >= 50) {
                    icon.innerHTML = '<i class="bi bi-cpu text-warning" style="font-size:16px"></i>';
                    icon.title = 'Moderate load: ' + pct.toFixed(0) + '%';
                } else {
                    icon.innerHTML = '<i class="bi bi-snow text-info" style="font-size:16px"></i>';
                    icon.title = 'Cool: ' + pct.toFixed(0) + '%';
                }

                // Color the CPU text
                if (pct >= 80) {
                    cpu.parentElement.className = 'text-danger fw-bold';
                } else if (pct >= 50) {
                    cpu.parentElement.className = 'text-warning';
                } else {
                    cpu.parentElement.className = '';
                }
            })
            .catch(function() { /* silent */ });
    }

    update();
    setInterval(update, 3000);
})();
