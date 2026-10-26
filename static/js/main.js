/* ==========================================================================
   MediVault — Main Frontend JavaScript Engine
   ========================================================================== */

document.addEventListener('DOMContentLoaded', function () {
    // 1. Sticky Navbar Scroll Effect (supports floating and standard navbars)
    const navbar = document.querySelector('.navbar-medivault, .navbar-medivault-float');
    if (navbar) {
        window.addEventListener('scroll', function () {
            if (window.scrollY > 20) {
                navbar.classList.add('scrolled');
            } else {
                navbar.classList.remove('scrolled');
            }
        });
    }

    // 2. Scroll Reveal Animations (IntersectionObserver)
    const revealElements = document.querySelectorAll('.reveal-on-scroll');
    if (revealElements.length > 0) {
        const observer = new IntersectionObserver((entries) => {
            entries.forEach(entry => {
                if (entry.isIntersecting) {
                    entry.target.classList.add('active');
                }
            });
        }, { threshold: 0.15 });

        revealElements.forEach(el => observer.observe(el));
    }

    // 3. Password Strength Indicator
    const passwordInput = document.getElementById('registerPassword');
    const strengthBar = document.getElementById('passwordStrengthBar');
    const strengthText = document.getElementById('passwordStrengthText');

    if (passwordInput && strengthBar) {
        passwordInput.addEventListener('input', function () {
            const val = passwordInput.value;
            let score = 0;
            if (val.length >= 8) score += 25;
            if (/[A-Z]/.test(val)) score += 25;
            if (/[0-9]/.test(val)) score += 25;
            if (/[^A-Za-z0-9]/.test(val)) score += 25;

            strengthBar.style.width = score + '%';

            if (score <= 25) {
                strengthBar.className = 'progress-bar bg-danger';
                strengthText.textContent = 'Weak Password';
            } else if (score <= 50) {
                strengthBar.className = 'progress-bar bg-warning';
                strengthText.textContent = 'Moderate Password';
            } else if (score <= 75) {
                strengthBar.className = 'progress-bar bg-info';
                strengthText.textContent = 'Good Password';
            } else {
                strengthBar.className = 'progress-bar bg-success';
                strengthText.textContent = 'Strong Password';
            }
        });
    }

    // 4. Password Show / Hide Toggle
    const toggleButtons = document.querySelectorAll('.toggle-password');
    toggleButtons.forEach(btn => {
        btn.addEventListener('click', function () {
            const targetId = this.getAttribute('data-target');
            const input = document.getElementById(targetId);
            if (input) {
                if (input.type === 'password') {
                    input.type = 'text';
                    this.innerHTML = '<i class="fas fa-eye-slash"></i>';
                } else {
                    input.type = 'password';
                    this.innerHTML = '<i class="fas fa-eye"></i>';
                }
            }
        });
    });

    // 5. Initialize Landing Page Charts (Supplier Price Chart, AI Forecast Chart, Snapshot Chart)
    initLandingCharts();
});

// Chart Initialization Helper
function initLandingCharts() {
    // Supplier Price History Chart
    const priceCtx = document.getElementById('supplierPriceChart');
    if (priceCtx) {
        new Chart(priceCtx, {
            type: 'line',
            data: {
                labels: ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun'],
                datasets: [
                    {
                        label: 'ABC Pharma (₹)',
                        data: [22, 21, 20, 19, 18, 18],
                        borderColor: '#00A896',
                        backgroundColor: 'rgba(0, 168, 150, 0.1)',
                        fill: true,
                        tension: 0.4
                    },
                    {
                        label: 'MedLife (₹)',
                        data: [17, 17, 18, 18, 19, 19],
                        borderColor: '#0077B6',
                        backgroundColor: 'transparent',
                        borderDash: [5, 5],
                        tension: 0.4
                    }
                ]
            },
            options: {
                responsive: true,
                plugins: {
                    legend: { position: 'bottom' },
                    title: { display: true, text: 'Paracetamol 500mg Batch Price Trends (6 Months)' }
                },
                scales: {
                    y: { beginAtZero: false, min: 15, max: 25 }
                }
            }
        });
    }

    // AI Demand Forecast Preview Chart
    const forecastCtx = document.getElementById('demandForecastChart');
    if (forecastCtx) {
        new Chart(forecastCtx, {
            type: 'bar',
            data: {
                labels: ['Week 1', 'Week 2', 'Week 3', 'Week 4'],
                datasets: [{
                    label: 'Predicted Medicine Units Required',
                    data: [1200, 1450, 1800, 2100],
                    backgroundColor: ['#028090', '#00A896', '#10B981', '#0077B6'],
                    borderRadius: 6
                }]
            },
            options: {
                responsive: true,
                plugins: {
                    legend: { display: false },
                    title: { display: true, text: 'AI Demand Forecast — Upcoming 30 Days' }
                }
            }
        });
    }

    // Pharmacy Snapshot Medicine Demand Trend Chart (Public Mock Preview)
    const snapshotCtx = document.getElementById('snapshotDemandChart');
    if (snapshotCtx) {
        new Chart(snapshotCtx, {
            type: 'line',
            data: {
                labels: ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun'],
                datasets: [{
                    label: 'Monthly Medicine Demand (Units)',
                    data: [1100, 1350, 1280, 1500, 1720, 1950],
                    borderColor: '#00A896',
                    backgroundColor: 'rgba(0, 168, 150, 0.12)',
                    fill: true,
                    tension: 0.35,
                    pointBackgroundColor: '#0077B6'
                }]
            },
            options: {
                responsive: true,
                plugins: {
                    legend: { display: true, position: 'top' },
                    title: { display: false }
                },
                scales: {
                    y: { beginAtZero: false }
                }
            }
        });
    }
}

// SweetAlert Notification Helper
function showAlert(title, text, icon = 'info') {
    if (typeof Swal !== 'undefined') {
        Swal.fire({
            title: title,
            text: text,
            icon: icon,
            confirmButtonColor: '#00A896',
            borderRadius: '16px'
        });
    } else {
        alert(`${title}: ${text}`);
    }
}
