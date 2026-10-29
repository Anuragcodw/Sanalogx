/**
 * Sanalogx — Professional Barcode & QR Scanner Engine
 * Supports:
 *  - Live Camera Barcode/QR Scanning via Html5Qrcode
 *  - Rear / Environment Camera preference with device switching
 *  - Flashlight / Torch toggle (when supported by device track)
 *  - Physical USB / Bluetooth Hardware Scanners (keyboard emulation with Enter suffix)
 *  - Duplicate scan throttling and non-deducting lookup resolution
 *  - Lifecycle management: track release on close / navigate away / tab hidden
 */

const MedivaultScanner = (function () {
    let html5QrCode = null;
    let availableCameras = [];
    let currentCameraIndex = 0;
    let isCameraRunning = false;
    let isScanLocked = false;
    let isTorchOn = false;
    let onCodeDecoded = null;
    let lastScanTime = 0;
    let lastScannedCode = '';
    const DEBOUNCE_MS = 1500;

    // ── Audio Feedback Chime (Web Audio API) ─────────────────────────────────
    function playChime() {
        try {
            const AudioCtx = window.AudioContext || window.webkitAudioContext;
            if (!AudioCtx) return;
            const ctx = new AudioCtx();
            const osc = ctx.createOscillator();
            const gain = ctx.createGain();
            osc.connect(gain);
            gain.connect(ctx.destination);
            osc.type = 'sine';
            osc.frequency.setValueAtTime(880, ctx.currentTime);
            gain.gain.setValueAtTime(0.12, ctx.currentTime);
            gain.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.15);
            osc.start();
            osc.stop(ctx.currentTime + 0.15);
        } catch (e) {
            // Audio blocked by browser policy
        }
    }

    // ── Helper: Safe Text Escaping ───────────────────────────────────────────
    function escapeHtml(str) {
        if (!str) return '';
        return String(str)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#039;');
    }

    // ── Status & UI Update Methods ───────────────────────────────────────────
    function updateStatus(text, isError = false, isSuccess = false) {
        const statusText = document.getElementById('scannerStatusText');
        const statusPill = document.getElementById('scannerStatusPill');
        const laser = document.getElementById('scannerLaser');
        const reticle = document.getElementById('scannerReticle');

        if (!statusText || !statusPill) return;
        statusText.innerText = text;

        if (isError) {
            statusPill.className = 'scanner-status-pill border border-danger text-danger bg-dark';
            if (laser) laser.style.display = 'none';
            if (reticle) reticle.style.borderColor = 'rgba(239, 68, 68, 0.8)';
        } else if (isSuccess) {
            statusPill.className = 'scanner-status-pill border border-success text-success bg-dark';
            if (laser) laser.style.display = 'none';
            if (reticle) reticle.style.borderColor = 'rgba(16, 185, 129, 0.9)';
        } else {
            statusPill.className = 'scanner-status-pill';
            if (laser) laser.style.display = 'block';
            if (reticle) reticle.style.borderColor = 'rgba(0, 169, 157, 0.6)';
        }
    }

    function showAlert(msg, type = 'warning') {
        const box = document.getElementById('cameraAlertBox');
        const text = document.getElementById('cameraAlertText');
        if (!box || !text) return;
        box.className = 'alert alert-' + type + ' py-2 px-3 small mb-3';
        text.innerHTML = msg;
        box.classList.remove('d-none');
    }

    function hideAlert() {
        const box = document.getElementById('cameraAlertBox');
        if (box) box.classList.add('d-none');
    }

    // ── Modal Lifecycle ──────────────────────────────────────────────────────
    function openModal(callback) {
        if (callback && typeof callback === 'function') {
            onCodeDecoded = callback;
        }
        const modalEl = document.getElementById('cameraScannerModal');
        if (!modalEl) {
            console.error('Camera scanner modal element not found in DOM.');
            return;
        }
        const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
        modal.show();
    }

    function closeModal() {
        const modalEl = document.getElementById('cameraScannerModal');
        if (modalEl) {
            const modal = bootstrap.Modal.getInstance(modalEl);
            if (modal) modal.hide();
        }
        stopCamera();
    }

    // ── Camera Engine ────────────────────────────────────────────────────────
    function startCamera() {
        hideAlert();
        if (isCameraRunning) return;

        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            showAlert('Your browser does not support camera capture or requires a secure context (HTTPS / localhost). Manual barcode entry is available below.', 'danger');
            updateStatus('Camera Not Supported', true);
            return;
        }

        updateStatus('Requesting Camera Access...');
        const btnStart = document.getElementById('btnStartCamera');
        const btnStop = document.getElementById('btnStopCamera');
        if (btnStart) btnStart.disabled = true;
        if (btnStop) btnStop.disabled = false;
        isScanLocked = false;

        if (!html5QrCode) {
            try {
                html5QrCode = new Html5Qrcode("cameraReader", { verbose: false });
            } catch (e) {
                showAlert('Scanner engine failed to initialize: ' + e.message, 'danger');
                updateStatus('Engine Error', true);
                return;
            }
        }

        Html5Qrcode.getCameras().then(devices => {
            availableCameras = devices || [];
            const switchBtn = document.getElementById('btnSwitchCamera');
            if (switchBtn) {
                switchBtn.style.display = (availableCameras.length > 1) ? 'inline-block' : 'none';
            }

            if (availableCameras.length === 0) {
                showAlert('No active camera hardware detected. You can type or paste barcodes manually using the input field.', 'info');
                updateStatus('No Camera Found', true);
                if (btnStart) btnStart.disabled = false;
                if (btnStop) btnStop.disabled = true;
                return;
            }

            const cameraConfig = (availableCameras.length > 0)
                ? { deviceId: { exact: availableCameras[currentCameraIndex % availableCameras.length].id } }
                : { facingMode: "environment" };

            const qrConfig = {
                fps: 15,
                qrbox: function (viewfinderWidth, viewfinderHeight) {
                    const minEdge = Math.min(viewfinderWidth, viewfinderHeight);
                    return {
                        width: Math.floor(minEdge * 0.74),
                        height: Math.floor(minEdge * 0.52)
                    };
                },
                aspectRatio: 1.333334
            };

            html5QrCode.start(
                cameraConfig,
                qrConfig,
                onScanSuccess,
                onScanFailure
            ).then(() => {
                isCameraRunning = true;
                if (btnStart) btnStart.disabled = true;
                if (btnStop) btnStop.disabled = false;
                updateStatus('Camera Active — Scanning...');
                checkTorchSupport();
            }).catch(err => {
                isCameraRunning = false;
                if (btnStart) btnStart.disabled = false;
                if (btnStop) btnStop.disabled = true;
                const errMsg = err.message || String(err);
                if (errMsg.includes('NotAllowedError') || errMsg.includes('Permission')) {
                    showAlert('<strong>Camera permission was denied.</strong> Click the lock or camera icon in your browser address bar to allow camera access, or use manual entry.', 'warning');
                } else if (errMsg.includes('NotFoundError') || errMsg.includes('DevicesNotFoundError')) {
                    showAlert('No video device found on this system.', 'info');
                } else {
                    showAlert('Unable to start camera stream (' + escapeHtml(errMsg) + ').', 'danger');
                }
                updateStatus('Camera Inactive', true);
            });
        }).catch(err => {
            showAlert('Device enumeration failed: ' + (err.message || err), 'danger');
            updateStatus('Device Error', true);
            if (btnStart) btnStart.disabled = false;
            if (btnStop) btnStop.disabled = true;
        });
    }

    function stopCamera() {
        if (html5QrCode && isCameraRunning) {
            html5QrCode.stop().then(() => {
                isCameraRunning = false;
                updateStatus('Camera Stopped');
                resetControlButtons();
            }).catch(() => {
                isCameraRunning = false;
                resetControlButtons();
            });
        } else {
            isCameraRunning = false;
            resetControlButtons();
        }
        isTorchOn = false;
        const torchBtn = document.getElementById('btnToggleTorch');
        if (torchBtn) {
            torchBtn.classList.remove('active');
            torchBtn.style.display = 'none';
        }
    }

    function resetControlButtons() {
        const btnStart = document.getElementById('btnStartCamera');
        const btnStop = document.getElementById('btnStopCamera');
        if (btnStart) btnStart.disabled = false;
        if (btnStop) btnStop.disabled = true;
    }

    function switchCamera() {
        if (availableCameras.length > 1) {
            currentCameraIndex = (currentCameraIndex + 1) % availableCameras.length;
            if (isCameraRunning && html5QrCode) {
                html5QrCode.stop().then(() => {
                    isCameraRunning = false;
                    startCamera();
                }).catch(() => {
                    isCameraRunning = false;
                    startCamera();
                });
            }
        }
    }

    function checkTorchSupport() {
        const torchBtn = document.getElementById('btnToggleTorch');
        if (!torchBtn || !html5QrCode) return;
        try {
            const track = html5QrCode.getRunningTrackCameraCapabilities();
            if (track && track.torchFeature && track.torchFeature().isSupported()) {
                torchBtn.style.display = 'inline-block';
            } else {
                torchBtn.style.display = 'none';
            }
        } catch (e) {
            torchBtn.style.display = 'none';
        }
    }

    function toggleTorch() {
        const torchBtn = document.getElementById('btnToggleTorch');
        if (!html5QrCode || !isCameraRunning) return;
        try {
            const track = html5QrCode.getRunningTrackCameraCapabilities();
            if (track && track.torchFeature) {
                isTorchOn = !isTorchOn;
                track.torchFeature().apply(isTorchOn);
                if (torchBtn) {
                    if (isTorchOn) torchBtn.classList.add('active');
                    else torchBtn.classList.remove('active');
                }
            }
        } catch (e) {
            console.warn('Torch toggle not supported on this track:', e);
        }
    }

    // ── Scan Callback Handlers ───────────────────────────────────────────────
    function onScanSuccess(decodedText) {
        if (isScanLocked) return;
        isScanLocked = true;

        playChime();
        updateStatus('Code Detected: ' + decodedText, false, true);

        const modalInput = document.getElementById('modalDecodedValue');
        if (modalInput) modalInput.value = decodedText;

        const badge = document.getElementById('decodedPillBadge');
        if (badge) {
            badge.className = 'badge bg-success font-monospace';
            badge.innerText = 'DECODED';
        }

        const btnVerify = document.getElementById('btnModalSearchVerify');
        if (btnVerify) btnVerify.disabled = false;

        // Pause continuous frames
        if (html5QrCode && isCameraRunning) {
            try { html5QrCode.pause(); } catch (e) { }
        }
    }

    function onScanFailure() {
        // Normal continuous scanning idle frame
    }

    function restartScanning() {
        isScanLocked = false;
        const modalInput = document.getElementById('modalDecodedValue');
        if (modalInput) modalInput.value = '';

        const badge = document.getElementById('decodedPillBadge');
        if (badge) {
            badge.className = 'badge bg-secondary font-monospace';
            badge.innerText = 'AWAITING CODE';
        }

        const btnVerify = document.getElementById('btnModalSearchVerify');
        if (btnVerify) btnVerify.disabled = true;

        updateStatus('Camera Active — Scanning...');

        if (html5QrCode) {
            try {
                html5QrCode.resume();
            } catch (e) {
                if (!isCameraRunning) startCamera();
            }
        } else {
            startCamera();
        }
    }

    function confirmAndLookup() {
        const modalInput = document.getElementById('modalDecodedValue');
        const code = modalInput ? modalInput.value.trim() : '';
        if (!code) return;

        closeModal();

        if (onCodeDecoded && typeof onCodeDecoded === 'function') {
            onCodeDecoded(code);
        }
    }

    // ── Hardware Scanner & Keyboard Integration ──────────────────────────────
    function attachHardwareScanner(inputElement, onScanCallback) {
        if (!inputElement) return;

        inputElement.addEventListener('focus', function () {
            this.classList.add('input-scanner-focused');
        });

        inputElement.addEventListener('blur', function () {
            this.classList.remove('input-scanner-focused');
        });

        inputElement.addEventListener('keypress', function (e) {
            if (e.key === 'Enter') {
                e.preventDefault();
                const code = this.value.trim();
                if (!code) return;

                // Debounce consecutive identical scans within 1.5s
                const now = Date.now();
                if (code === lastScannedCode && (now - lastScanTime) < DEBOUNCE_MS) {
                    console.log('Debounced duplicate hardware scan:', code);
                    return;
                }
                lastScanTime = now;
                lastScannedCode = code;

                // Visual flash on input
                this.classList.add('input-scanner-success');
                setTimeout(() => this.classList.remove('input-scanner-success'), 600);

                playChime();
                if (onScanCallback && typeof onScanCallback === 'function') {
                    onScanCallback(code);
                }
            }
        });
    }

    // ── Global Event Bindings on DOM Ready ───────────────────────────────────
    document.addEventListener('DOMContentLoaded', function () {
        const modalEl = document.getElementById('cameraScannerModal');
        if (modalEl) {
            modalEl.addEventListener('shown.bs.modal', function () {
                startCamera();
            });
            modalEl.addEventListener('hidden.bs.modal', function () {
                stopCamera();
            });
        }

        window.addEventListener('beforeunload', function () {
            stopCamera();
        });

        document.addEventListener('visibilitychange', function () {
            if (document.hidden) {
                stopCamera();
            }
        });
    });

    // Public API
    return {
        openModal: openModal,
        closeModal: closeModal,
        startCamera: startCamera,
        stopCamera: stopCamera,
        switchCamera: switchCamera,
        toggleTorch: toggleTorch,
        restartScanning: restartScanning,
        confirmAndLookup: confirmAndLookup,
        attachHardwareScanner: attachHardwareScanner,
        playChime: playChime,
        escapeHtml: escapeHtml
})();
window.SanalogxScanner = MedivaultScanner;
