/**
 * MediVault — Admin Dashboard Interactive Behaviors
 * Sidebar toggle, mobile drawer, hash tab persistence, and dismissible alerts.
 */

(function () {
    'use strict';

    document.addEventListener('DOMContentLoaded', function () {
        var appContainer = document.getElementById('adminAppContainer');
        var sidebarToggle = document.getElementById('adminSidebarToggle');
        var mobileBackdrop = document.getElementById('adminSidebarBackdrop');

        // 1. Sidebar Toggle (Desktop Collapse & Mobile Drawer)
        if (sidebarToggle && appContainer) {
            sidebarToggle.addEventListener('click', function () {
                if (window.innerWidth >= 992) {
                    appContainer.classList.toggle('sidebar-collapsed');
                    var isCollapsed = appContainer.classList.contains('sidebar-collapsed');
                    try {
                        localStorage.setItem('medivault_admin_sidebar_collapsed', isCollapsed ? '1' : '0');
                    } catch (_) {}
                } else {
                    appContainer.classList.toggle('mobile-open');
                }
            });
        }

        // Restore collapse state on desktop
        if (appContainer && window.innerWidth >= 992) {
            try {
                if (localStorage.getItem('medivault_admin_sidebar_collapsed') === '1') {
                    appContainer.classList.add('sidebar-collapsed');
                }
            } catch (_) {}
        }

        // 2. Mobile Backdrop Click to Close
        if (mobileBackdrop && appContainer) {
            mobileBackdrop.addEventListener('click', function () {
                appContainer.classList.remove('mobile-open');
            });
        }

        // Close on Esc key for accessibility
        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape' && appContainer && appContainer.classList.contains('mobile-open')) {
                appContainer.classList.remove('mobile-open');
            }
        });

        // Close drawer on nav item click on mobile
        var navLinks = document.querySelectorAll('.admin-nav-link');
        navLinks.forEach(function (link) {
            link.addEventListener('click', function () {
                if (window.innerWidth < 992 && appContainer) {
                    appContainer.classList.remove('mobile-open');
                }
            });
        });

        // 3. Tab Hash Sync (e.g. #approved-pane or #audit-pane)
        var hash = window.location.hash;
        if (hash) {
            var targetTabTrigger = document.querySelector('[data-bs-target="' + hash + '"]');
            if (targetTabTrigger && window.bootstrap && window.bootstrap.Tab) {
                var tab = new bootstrap.Tab(targetTabTrigger);
                tab.show();
            }
        }

        // Update hash when tab clicked
        var tabButtons = document.querySelectorAll('[data-bs-toggle="tab"]');
        tabButtons.forEach(function (btn) {
            btn.addEventListener('shown.bs.tab', function (e) {
                var target = e.target.getAttribute('data-bs-target');
                if (target && history.replaceState) {
                    history.replaceState(null, null, target);
                }
            });
        });

        // 4. Auto-dismiss alerts after 6 seconds (if present)
        var alertBanners = document.querySelectorAll('.admin-alert-banner');
        alertBanners.forEach(function (alert) {
            setTimeout(function () {
                alert.style.opacity = '0';
                alert.style.transform = 'translateY(-10px)';
                setTimeout(function () {
                    alert.remove();
                }, 300);
            }, 6000);
        });

        // 5. Fetch Unread Notifications for Header Bell
        var bellBadge = document.getElementById('adminHeaderNotifBadge');
        var notifList = document.getElementById('adminHeaderNotifList');
        if (bellBadge) {
            fetch('/api/notifications/bell/')
                .then(function (res) { return res.json(); })
                .then(function (data) {
                    if (data && data.unread_count > 0) {
                        bellBadge.textContent = data.unread_count;
                        bellBadge.classList.remove('d-none');
                    } else {
                        bellBadge.classList.add('d-none');
                    }
                    if (notifList && data && data.notifications && data.notifications.length > 0) {
                        var html = '';
                        data.notifications.forEach(function (n) {
                            var url = n.action_url || '/notifications/';
                            html += '<a href="' + url + '" class="dropdown-item p-2 border-bottom text-wrap">' +
                                    '<strong class="d-block text-navy small">' + n.title + '</strong>' +
                                    '<p class="text-muted extra-small mb-0 text-truncate" style="max-width: 260px;">' + n.message + '</p>' +
                                    '</a>';
                        });
                        notifList.innerHTML = html;
                    } else if (notifList) {
                        notifList.innerHTML = '<div class="text-center py-3 text-muted small">No unread notifications</div>';
                    }
                })
                .catch(function () {});
        }
    });
}());
