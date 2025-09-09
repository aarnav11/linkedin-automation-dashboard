function initializeCampaign(campaignId) {
    if (!campaignId) return;

    let campaignRunning = false;
    let pollInterval = null;
    const startBtn = document.getElementById('start-campaign');
    const stopBtn = document.getElementById('stop-campaign');
    const messageTextarea = document.getElementById('preview-message');
    const confirmSendBtn = document.getElementById('confirm-send');
    const confirmSkipBtn = document.getElementById('confirm-skip');
    const messagePreviewModal = new bootstrap.Modal(document.getElementById('messagePreviewModal'));


    // --- Event Listeners ---
    startBtn?.addEventListener('click', startCampaign);
    stopBtn?.addEventListener('click', stopCampaign);
    messageTextarea?.addEventListener('input', updateMessageLength);
    confirmSendBtn?.addEventListener('click', handleSendAction);
    confirmSkipBtn?.addEventListener('click', handleSkipAction);


    // --- Core Functions ---
    function startCampaign() {
        fetch('/start_campaign', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ campaign_id: campaignId })
        })
        .then(res => res.json())
        .then(data => {
            if (data.success) {
                campaignRunning = true;
                startBtn.style.display = 'none';
                stopBtn.style.display = 'inline-block';
                document.getElementById('campaign-status').style.display = 'block';
                pollInterval = setInterval(pollCampaignStatus, 3000); // Poll every 3 seconds
                showAlert('Campaign started successfully!', 'success');
            } else {
                showAlert(`Failed to start campaign: ${data.error || 'Unknown error'}`, 'danger');
            }
        })
        .catch(err => showAlert(`Error starting campaign: ${err}`, 'danger'));
    }

    function stopCampaign() {
        fetch('/stop_campaign', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ campaign_id: campaignId })
        })
        .then(res => res.json())
        .then(data => {
            if (data.success) {
                campaignRunning = false;
                clearInterval(pollInterval);
                stopBtn.style.display = 'none';
                startBtn.style.display = 'inline-block';
                document.getElementById('activity-text').innerHTML = 'Campaign stopped by user.';
                document.getElementById('status-badge').textContent = 'Stopped';
                document.getElementById('status-badge').className = 'badge ms-2 bg-warning';
                showAlert('Campaign stop request sent.', 'warning');
            }
        })
        .catch(err => showAlert(`Error stopping campaign: ${err}`, 'danger'));
    }

    function pollCampaignStatus() {
        if (!campaignRunning) return;

        fetch(`/campaign_results/${campaignId}`)
            .then(res => res.json())
            .then(data => {
                if (!data) return;
                updateProgressDisplay(data);
                if (data.awaiting_confirmation && data.current_contact_preview) {
                    showMessagePreview(data.current_contact_preview);
                }
                if (data.status === 'completed' || data.status === 'failed' || data.status === 'stopped') {
                    campaignRunning = false;
                    clearInterval(pollInterval);
                    stopBtn.style.display = 'none';
                    startBtn.style.display = 'inline-block';
                    showAlert(`Campaign ${data.status}!`, data.status === 'completed' ? 'success' : 'danger');
                }
            })
            .catch(err => console.error('Polling error:', err));
    }

    function handleSendAction() {
        const message = messageTextarea.value;
        if (message.length > 280) {
            alert('Message is too long! Please keep it under 280 characters.');
            return;
        }
        const contactIndex = document.getElementById('messagePreviewModal').dataset.contactIndex;
        sendMessageAction('send', message, contactIndex);
    }

    function handleSkipAction() {
        const contactIndex = document.getElementById('messagePreviewModal').dataset.contactIndex;
        sendMessageAction('skip', '', contactIndex);
    }

    function sendMessageAction(action, message, contactIndex) {
        fetch('/confirm_message_action', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                campaign_id: campaignId,
                action: action,
                message: message,
                contact_index: parseInt(contactIndex)
            })
        })
        .then(res => res.json())
        .then(data => {
            if (data.success) {
                messagePreviewModal.hide();
                showAlert(action === 'send' ? 'Message approved for sending!' : 'Contact skipped.', 'info');
            } else {
                showAlert(`Error processing action: ${data.error || 'Unknown error'}`, 'danger');
            }
        })
        .catch(err => showAlert(`Error sending action: ${err}`, 'danger'));
    }

    // --- UI Helper Functions ---
    function updateProgressDisplay(data) {
        document.getElementById('successful-count').textContent = data.successful || 0;
        document.getElementById('failed-count').textContent = data.failed || 0;
        document.getElementById('skipped-count').textContent = data.skipped || 0;
        document.getElementById('already-messaged-count').textContent = data.already_messaged || 0;

        const current = data.progress || 0;
        const total = data.total || 1;
        const percentage = Math.round((current / total) * 100);
        document.getElementById('progress-current').textContent = current;
        document.getElementById('progress-total').textContent = total;
        document.getElementById('progress-bar').style.width = `${percentage}%`;
        document.getElementById('progress-bar').textContent = `${percentage}%`;

        const statusBadge = document.getElementById('status-badge');
        const activityText = document.getElementById('activity-text');
        const currentActivity = document.getElementById('current-activity');
        statusBadge.textContent = (data.status || '...').replace('_', ' ');
        statusBadge.className = `badge ms-2 ${getStatusBadgeClass(data.status)}`;

        if (data.awaiting_confirmation) {
            currentActivity.style.display = 'block';
            activityText.innerHTML = '<i class="fas fa-user-clock me-2"></i>Waiting for user confirmation...';
        } else if (data.status === 'running') {
            currentActivity.style.display = 'block';
            activityText.innerHTML = '<i class="fas fa-spinner fa-spin me-2"></i>Processing contacts...';
        } else {
            currentActivity.style.display = 'none';
        }
    }

    function showMessagePreview(previewData) {
        const { contact, message, contact_index } = previewData;
        document.getElementById('preview-name').textContent = contact.Name || '-';
        document.getElementById('preview-company').textContent = contact.Company || '-';
        document.getElementById('preview-role').textContent = contact.Role || '-';
        document.getElementById('preview-linkedin').href = contact.LinkedIn_profile || '#';
        messageTextarea.value = message || '';
        document.getElementById('messagePreviewModal').dataset.contactIndex = contact_index || 0;
        updateMessageLength();
        messagePreviewModal.show();
    }

    function updateMessageLength() {
        const length = messageTextarea.value.length;
        const lengthSpan = document.getElementById('message-length');
        lengthSpan.textContent = length;
        lengthSpan.className = length > 280 ? 'text-danger' : length > 250 ? 'text-warning' : 'text-muted';
    }

    function getStatusBadgeClass(status) {
        const classes = {
            running: 'bg-primary',
            completed: 'bg-success',
            failed: 'bg-danger',
            stopped: 'bg-warning text-dark',
            awaiting_user_action: 'bg-info text-dark',
            logging_in: 'bg-secondary',
        };
        return classes[status] || 'bg-secondary';
    }

    function showAlert(message, type = 'info') {
        const wrapper = document.createElement('div');
        wrapper.innerHTML = `
            <div class="alert alert-${type} alert-dismissible fade show" role="alert">
                ${message}
                <button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="Close"></button>
            </div>
        `;
        document.querySelector('.container').prepend(wrapper);
        setTimeout(() => {
            const alert = bootstrap.Alert.getOrCreateInstance(wrapper.querySelector('.alert'));
            alert.close();
        }, 5000);
    }
}