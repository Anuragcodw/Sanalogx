/**
 * static/js/medivault_ocr.js
 * Sanalogx — AI-Assisted Medicine Packaging OCR Engine
 * Handles image selection, client-side validation, server-side OCR processing,
 * candidate match display with confidence scoring, and safe confirmation workflows.
 */

const MedivaultOCR = (function () {
    let onCandidateSelectedCallback = null;
    let selectedFile = null;
    const MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024; // 5 MB

    function showOcrAlert(message, type = 'warning') {
        const alertBox = document.getElementById('ocrModalAlertBox');
        const alertText = document.getElementById('ocrModalAlertText');
        if (!alertBox || !alertText) return;
        alertBox.className = 'alert alert-' + type + ' py-2 px-3 small mb-3';
        alertText.innerHTML = message;
        alertBox.classList.remove('d-none');
    }

    function hideOcrAlert() {
        const alertBox = document.getElementById('ocrModalAlertBox');
        if (alertBox) alertBox.classList.add('d-none');
    }

    function openModal(callback) {
        if (callback && typeof callback === 'function') {
            onCandidateSelectedCallback = callback;
        }
        hideOcrAlert();
        resetOcrImage();
        const modalEl = document.getElementById('ocrPackagingModal');
        if (modalEl) {
            const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
            modal.show();
        }
    }

    function closeModal() {
        const modalEl = document.getElementById('ocrPackagingModal');
        if (modalEl) {
            const modal = bootstrap.Modal.getInstance(modalEl);
            if (modal) modal.hide();
        }
    }

    function resetOcrImage() {
        selectedFile = null;
        const fileInput = document.getElementById('ocrFileInput');
        if (fileInput) fileInput.value = '';

        const previewContainer = document.getElementById('ocrPreviewContainer');
        const promptContainer = document.getElementById('ocrPrompt');
        const resultsSection = document.getElementById('ocrResultsSection');
        const spinner = document.getElementById('ocrLoadingSpinner');

        if (previewContainer) previewContainer.classList.add('d-none');
        if (promptContainer) promptContainer.classList.remove('d-none');
        if (resultsSection) resultsSection.classList.add('d-none');
        if (spinner) spinner.classList.add('d-none');
        hideOcrAlert();
    }

    function handleFileSelected(event) {
        hideOcrAlert();
        const file = event.target.files && event.target.files[0];
        if (!file) return;

        // Size check
        if (file.size > MAX_FILE_SIZE_BYTES) {
            showOcrAlert(`Image size (${(file.size / (1024 * 1024)).toFixed(1)} MB) exceeds the maximum allowed limit of 5 MB.`, 'danger');
            event.target.value = '';
            return;
        }

        // Type check
        const validTypes = ['image/jpeg', 'image/png', 'image/webp'];
        if (!validTypes.includes(file.type)) {
            showOcrAlert('Unsupported file format. Please upload a JPEG, PNG, or WEBP image.', 'danger');
            event.target.value = '';
            return;
        }

        selectedFile = file;

        // Show preview
        const reader = new FileReader();
        reader.onload = function (e) {
            const previewImg = document.getElementById('ocrImagePreview');
            const previewContainer = document.getElementById('ocrPreviewContainer');
            const promptContainer = document.getElementById('ocrPrompt');

            if (previewImg) previewImg.src = e.target.result;
            if (previewContainer) previewContainer.classList.remove('d-none');
            if (promptContainer) promptContainer.classList.add('d-none');

            // Trigger upload & OCR processing automatically upon selection
            processImage(file);
        };
        reader.readAsDataURL(file);
    }

    function processImage(file) {
        const spinner = document.getElementById('ocrLoadingSpinner');
        const resultsSection = document.getElementById('ocrResultsSection');
        if (spinner) spinner.classList.remove('d-none');
        if (resultsSection) resultsSection.classList.add('d-none');

        const formData = new FormData();
        formData.append('image', file);

        // Get CSRF Token
        let csrfToken = '';
        if (typeof CSRF_TOKEN !== 'undefined') {
            csrfToken = CSRF_TOKEN;
        } else {
            const el = document.querySelector('[name=csrfmiddlewaretoken]');
            if (el) csrfToken = el.value;
        }

        fetch('/medicines/barcodes/ocr/', {
            method: 'POST',
            headers: {
                'X-CSRFToken': csrfToken
            },
            body: formData
        })
        .then(response => {
            return response.json().then(data => ({ status: response.status, data }));
        })
        .then(({ status, data }) => {
            if (spinner) spinner.classList.add('d-none');

            if (!data.success) {
                showOcrAlert(data.message || 'OCR processing failed.', 'danger');
                return;
            }

            renderOcrResults(data);
        })
        .catch(err => {
            if (spinner) spinner.classList.add('d-none');
            showOcrAlert('Network error during OCR communication: ' + err.message, 'danger');
        });
    }

    function renderOcrResults(data) {
        const resultsSection = document.getElementById('ocrResultsSection');
        const rawTextContent = document.getElementById('ocrRawTextContent');
        const paramsBar = document.getElementById('ocrExtractedParamsBar');
        const candidatesCount = document.getElementById('ocrCandidatesCount');
        const candidatesList = document.getElementById('ocrCandidatesList');

        if (!resultsSection) return;

        // 1. Raw Text
        if (rawTextContent) {
            rawTextContent.innerText = data.raw_text || '(No readable text extracted)';
        }

        // 2. Extracted Params Badges
        if (paramsBar) {
            let tags = [];
            const fields = data.candidate_fields || {};
            if (fields.strengths && fields.strengths.length > 0) {
                tags.push(`<span><i class="fas fa-weight-hanging text-teal me-1"></i>Strengths: <strong>${fields.strengths.join(', ')}</strong></span>`);
            }
            if (fields.dosage_forms && fields.dosage_forms.length > 0) {
                tags.push(`<span><i class="fas fa-tablets text-teal me-1"></i>Forms: <strong>${fields.dosage_forms.join(', ')}</strong></span>`);
            }
            if (fields.batch_numbers && fields.batch_numbers.length > 0) {
                tags.push(`<span><i class="fas fa-hashtag text-teal me-1"></i>Detected Batch: <strong class="font-monospace text-primary">${fields.batch_numbers.join(', ')}</strong></span>`);
            }
            if (fields.expiry_dates && fields.expiry_dates.length > 0) {
                tags.push(`<span><i class="fas fa-calendar-xmark text-teal me-1"></i>Detected Expiry: <strong class="text-danger">${fields.expiry_dates.join(', ')}</strong></span>`);
            }
            paramsBar.innerHTML = tags.length > 0 ? tags.join(' &bull; ') : '<span class="text-muted">No specific dosage/batch patterns detected. Matching against broad catalog...</span>';
        }

        // 3. Candidates Count
        const count = (data.candidates && data.candidates.length) || 0;
        if (candidatesCount) {
            candidatesCount.innerText = `${count} Match${count === 1 ? '' : 'es'}`;
            candidatesCount.className = count > 0 ? 'badge bg-teal text-white' : 'badge bg-secondary';
        }

        // 4. Candidates Cards
        if (candidatesList) {
            if (count === 0) {
                candidatesList.innerHTML = `
                    <div class="text-center p-3 bg-light rounded border text-muted small">
                        <i class="fas fa-circle-question fs-4 text-muted mb-1 d-block"></i>
                        No matching medicines found in catalog. Check raw text above or try a clearer image.
                    </div>
                `;
            } else {
                candidatesList.innerHTML = data.candidates.map(cand => {
                    let confBadgeClass = 'bg-success';
                    if (cand.confidence_level === 'MEDIUM') confBadgeClass = 'bg-warning text-dark';
                    else if (cand.confidence_level === 'LOW') confBadgeClass = 'bg-secondary';

                    const batchInfo = cand.matched_batch_number
                        ? `<span class="badge bg-teal-subtle text-teal border extra-small font-monospace"><i class="fas fa-box me-1"></i>Batch Matched: ${cand.matched_batch_number}</span>`
                        : '';

                    return `
                        <div class="card bg-white border p-3 shadow-2xs" style="border-radius: 10px;">
                            <div class="d-flex justify-content-between align-items-start mb-1">
                                <div>
                                    <h6 class="fw-bold text-navy mb-0">${escapeHtml(cand.medicine_name)}</h6>
                                    <span class="text-muted extra-small">${escapeHtml(cand.generic_name)} · ${escapeHtml(cand.strength)} · ${escapeHtml(cand.dosage_form)}</span>
                                </div>
                                <div class="d-flex flex-column align-items-end gap-1">
                                    <span class="badge ${confBadgeClass}">${cand.confidence_level} CONFIDENCE (${cand.confidence_score}%)</span>
                                    <span class="badge bg-light text-muted border font-monospace extra-small">${escapeHtml(cand.internal_barcode)}</span>
                                </div>
                            </div>
                            <div class="d-flex flex-wrap gap-2 text-muted extra-small mb-2">
                                <span><i class="fas fa-tags text-teal me-1"></i>${escapeHtml(cand.category)}</span>
                                <span><i class="fas fa-industry text-teal me-1"></i>${escapeHtml(cand.manufacturer)}</span>
                                <span><i class="fas fa-boxes-stacked text-teal me-1"></i>Usable Stock: <strong>${cand.usable_stock}</strong></span>
                                ${batchInfo}
                            </div>
                            <div class="d-flex justify-content-end gap-2">
                                <button type="button" class="btn btn-teal btn-xs px-3 py-1 fw-semibold" onclick="MedivaultOCR.confirmCandidate('${escapeHtml(cand.internal_barcode || cand.medicine_id)}')">
                                    <i class="fas fa-check-circle me-1"></i> Confirm &amp; View Profile
                                </button>
                            </div>
                        </div>
                    `;
                }).join('');
            }
        }

        resultsSection.classList.remove('d-none');
    }

    function confirmCandidate(barcodeOrId) {
        closeModal();
        if (onCandidateSelectedCallback && typeof onCandidateSelectedCallback === 'function') {
            onCandidateSelectedCallback(barcodeOrId);
        } else if (typeof performScanLookup === 'function') {
            const input = document.getElementById('barcodeInput');
            if (input) input.value = barcodeOrId;
            performScanLookup(barcodeOrId);
        }
    }

    function escapeHtml(str) {
        if (!str) return '';
        return String(str)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#039;');
    }

    return {
        openModal: openModal,
        closeModal: closeModal,
        handleFileSelected: handleFileSelected,
        resetOcrImage: resetOcrImage,
        confirmCandidate: confirmCandidate,
    };
})();

// Global helper bindings
function openOcrPackagingModal(callback) {
    MedivaultOCR.openModal(callback);
}

function closeOcrPackagingModal() {
    MedivaultOCR.closeModal();
}

function handleOcrFileSelected(event) {
    MedivaultOCR.handleFileSelected(event);
}

function resetOcrImage() {
    MedivaultOCR.resetOcrImage();
}
