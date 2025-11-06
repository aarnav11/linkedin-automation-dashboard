// static/js/campaign_logic.js - Enhanced version with better approval flow

let campaignId = null;
let statusCheckInterval = null;
let previewModal = null;
let currentContactData = null;

function initializeCampaign(cId) {
    campaignId = cId;
    console.log('Initializing campaign:', campaignId);
    
    // Initialize modal
    previewModal = new bootstrap.Modal(document.getElementById('messagePreviewModal'));
    
    // Add event listeners
    setupEventListeners();
    
    // Start status checking if campaign is active
    if (campaignId) {
        startStatusChecking();
    }
}

function setupEventListeners() {
    // Start campaign button
    const startBtn = document.getElementById('start-campaign');
    if (startBtn) {
        startBtn.addEventListener('click', startCampaign);
    }
    
    // Stop campaign button
    const stopBtn = document.getElementById('stop-campaign');
    if (stopBtn) {
        stopBtn.addEventListener('click', stopCampaign);
    }
    
    // Modal action buttons
    const confirmSendBtn = document.getElementById('confirm-send');
    const confirmSkipBtn = document.getElementById('confirm-skip');
    
    if (confirmSendBtn) {
        confirmSendBtn.addEventListener('click', () => handleCampaignAction('send'));
    }
    
    if (confirmSkipBtn) {
        confirmSkipBtn.addEventListener('click', () => handleCampaignAction('skip'));
    }
    
    // Message length counter
    const messageTextarea = document.getElementById('preview-message');
    if (messageTextarea) {
        messageTextarea.addEventListener('input', updateMessageLength);
    }
    
    // Handle modal close events
    const modal = document.getElementById('messagePreviewModal');
    if (modal) {
        modal.addEventListener('hidden.bs.modal', function () {
            // If modal is closed without action, treat as skip
            if (currentContactData && !currentContactData.actionTaken) {
                console.log('Modal closed without action, treating as skip');
                handleCampaignAction('skip');
            }
        });
    }
}

function startCampaign() {
    if (!campaignId) {
        showAlert('No campaign ID found', 'error');
        return;
    }
    
    console.log('Starting campaign:', campaignId);
    
    // Show loading state
    const startBtn = document.getElementById('start-campaign');
    if (startBtn) {
        startBtn.disabled = true;
        startBtn.innerHTML = '<i class="fas fa-spinner fa-spin me-2"></i>Starting...';
    }
    
    fetch('/start_campaign', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({ campaign_id: campaignId })
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            showAlert('Campaign started successfully! The client will begin processing contacts.', 'success');
            
            // Update UI
            document.getElementById('start-campaign').style.display = 'none';
            document.getElementById('stop-campaign').style.display = 'inline-block';
            document.getElementById('campaign-status').style.display = 'block';
            
            // Start status checking
            startStatusChecking();
        } else {
            showAlert(data.error || 'Failed to start campaign', 'error');
            // Reset button
            if (startBtn) {
                startBtn.disabled = false;
                startBtn.innerHTML = '<i class="fas fa-play-circle me-2"></i>Start Campaign';
            }
        }
    })
    .catch(error => {
        console.error('Error starting campaign:', error);
        showAlert('Error starting campaign: ' + error.message, 'error');
        // Reset button
        if (startBtn) {
            startBtn.disabled = false;
            startBtn.innerHTML = '<i class="fas fa-play-circle me-2"></i>Start Campaign';
        }
    });
}

function stopCampaign() {
    if (!campaignId) return;

    const stopBtn = document.getElementById('stop-campaign');
    if (stopBtn) {
        stopBtn.disabled = true;
        stopBtn.innerHTML = '<i class="fas fa-spinner fa-spin me-2"></i>Stopping...';
    }

    if (!confirm("Are you sure you want to stop this campaign?")) {
        if (stopBtn) {
            stopBtn.disabled = false;
            stopBtn.innerHTML = '<i class="fas fa-stop-circle me-2"></i>Stop Campaign';
        }
        return;
    }

    // Use unified stop_task endpoint instead of /stop_campaign
    fetch(`/stop_task/${campaignId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' }
    })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                showAlert('⏹️ Campaign stopped successfully!', 'info');
                stopStatusChecking();

                // Reset UI to idle state
                const startBtn = document.getElementById('start-campaign');
                if (startBtn) startBtn.style.display = 'inline-block';
                if (stopBtn) stopBtn.style.display = 'none';
                document.getElementById('status-badge').innerHTML = '⏹️ Stopped';
            } else {
                showAlert(data.message || '⚠️ Failed to stop campaign.', 'error');
            }
        })
        .catch(error => {
            console.error('Error stopping campaign:', error);
            showAlert('Network error while stopping campaign: ' + error.message, 'error');
        })
        .finally(() => {
            if (stopBtn) {
                stopBtn.disabled = false;
                stopBtn.innerHTML = '<i class="fas fa-stop-circle me-2"></i>Stop Campaign';
            }
        });
}


function startStatusChecking() {
    if (statusCheckInterval) {
        clearInterval(statusCheckInterval);
    }
    
    statusCheckInterval = setInterval(() => {
        checkCampaignStatus();
    }, 2000); // Check every 2 seconds for better responsiveness
    
    // Initial check
    checkCampaignStatus();
}

function stopStatusChecking() {
    if (statusCheckInterval) {
        clearInterval(statusCheckInterval);
        statusCheckInterval = null;
    }
}

function checkCampaignStatus() {
    if (!campaignId) return;
    
    fetch(`/api/campaign-progress/${campaignId}`)
    .then(response => response.json())
    .then(data => {
        console.log('Campaign status update:', data);
        updateCampaignUI(data);
        
        // Check if awaiting confirmation
        if (data.awaiting_confirmation && data.current_contact_preview) {
            // Only show modal if it's a new contact (prevent duplicate modals)
            if (!currentContactData || 
                currentContactData.contact_index !== data.current_contact_preview.contact_index) {
                showMessagePreview(data.current_contact_preview);
            }
        } else {
            // Clear current contact data if not awaiting
            currentContactData = null;
        }
    })
    .catch(error => {
        console.error('Error checking campaign status:', error);
        // Don't show error alerts for status checks to avoid spam
    });
}

function updateCampaignUI(data) {
    // Update progress counters with animation
    updateCounterAnimated('successful-count', data.successful || 0);
    updateCounterAnimated('failed-count', data.failed || 0);
    updateCounterAnimated('skipped-count', data.skipped || 0);
    updateCounterAnimated('already-messaged-count', data.already_messaged || 0);
    
    // Update progress bar
    const progress = data.progress || 0;
    const total = data.total || 0;
    const percentage = total > 0 ? (progress / total) * 100 : 0;
    
    const progressBar = document.getElementById('progress-bar');
    if (progressBar) {
        progressBar.style.width = `${percentage}%`;
        progressBar.setAttribute('aria-valuenow', percentage);
    }
    
    // Update progress text
    updateElement('progress-current', progress);
    updateElement('progress-total', total);
    
    // Update status badge
    const statusBadge = document.getElementById('status-badge');
    if (statusBadge) {
        const status = data.status || 'running';
        const statusText = status.charAt(0).toUpperCase() + status.slice(1);
        
        // Add emoji for visual appeal
        const statusEmojis = {
            'running': '🚀',
            'completed': '✅',
            'stopped': '⏹️',
            'failed': '❌',
            'paused': '⏸️'
        };
        
        statusBadge.innerHTML = `${statusEmojis[status.toLowerCase()] || '📊'} ${statusText}`;
        statusBadge.className = `badge bg-${getStatusColor(status)}`;
    }
    
    // Update current activity
    const activityDiv = document.getElementById('current-activity');
    const activityText = document.getElementById('activity-text');
    
    if (data.awaiting_confirmation && data.current_contact_preview) {
        if (activityDiv) activityDiv.style.display = 'block';
        if (activityText) {
            const contact = data.current_contact_preview.contact || {};
            activityText.innerHTML = `
                <i class="fas fa-user-check me-2"></i>
                Awaiting your decision for <strong>${contact.Name || 'contact'}</strong>...
            `;
        }
    } else if (data.status === 'running') {
        if (activityDiv) activityDiv.style.display = 'block';
        if (activityText) {
            activityText.innerHTML = `
                <i class="fas fa-cogs me-2"></i>
                Processing contacts... ${progress}/${total}
            `;
        }
    } else {
        if (activityDiv) activityDiv.style.display = 'none';
    }
    
    // Stop checking if campaign is complete
    if (['completed', 'stopped', 'failed'].includes(data.status)) {
        stopStatusChecking();
        document.getElementById('start-campaign').style.display = 'inline-block';
        document.getElementById('stop-campaign').style.display = 'none';
        
        // Show completion message
        const completionMessages = {
            'completed': `🎉 Campaign completed! Processed ${progress}/${total} contacts.`,
            'stopped': `⏹️ Campaign stopped by user at ${progress}/${total} contacts.`,
            'failed': `❌ Campaign failed at ${progress}/${total} contacts.`
        };
        
        if (completionMessages[data.status]) {
            showAlert(completionMessages[data.status], 
                     data.status === 'completed' ? 'success' : 
                     data.status === 'stopped' ? 'warning' : 'danger');
        }
    }
}

function showMessagePreview(previewData) {
    const contact = previewData.contact || {};
    const message = previewData.message || '';
    const contactIndex = previewData.contact_index || 0;
    
    console.log('Showing message preview for:', contact.Name, 'Index:', contactIndex);
    
    // Store current contact data
    currentContactData = {
        contact: contact,
        message: message,
        contact_index: contactIndex,
        actionTaken: false
    };
    
    // Update modal content with better formatting
    updateElement('preview-name', contact.Name || 'Unknown');
    updateElement('preview-company', contact.Company || 'Unknown');
    updateElement('preview-role', contact.Role || 'Unknown');
    
    const linkedinLink = document.getElementById('preview-linkedin');
    if (linkedinLink && contact.LinkedIn_profile) {
        linkedinLink.href = contact.LinkedIn_profile;
        linkedinLink.style.display = 'inline-block';
    } else if (linkedinLink) {
        linkedinLink.style.display = 'none';
    }
    
    // Set message with better formatting
    const messageTextarea = document.getElementById('preview-message');
    if (messageTextarea) {
        messageTextarea.value = message;
        updateMessageLength();
        
        // Store contact index for actions
        messageTextarea.dataset.contactIndex = contactIndex;
    }
    
    // Enable action buttons
    const confirmSendBtn = document.getElementById('confirm-send');
    const confirmSkipBtn = document.getElementById('confirm-skip');
    
    if (confirmSendBtn) {
        confirmSendBtn.disabled = false;
        confirmSendBtn.innerHTML = '<i class="fas fa-paper-plane me-2"></i>Approve & Send';
    }
    if (confirmSkipBtn) {
        confirmSkipBtn.disabled = false;
        confirmSkipBtn.innerHTML = '<i class="fas fa-forward me-2"></i>Skip Contact';
    }
    
    // Show modal with focus
    if (previewModal) {
        previewModal.show();
        
        // Focus on textarea after modal is shown
        setTimeout(() => {
            if (messageTextarea) {
                messageTextarea.focus();
                messageTextarea.setSelectionRange(messageTextarea.value.length, messageTextarea.value.length);
            }
        }, 300);
    }
}

function handleCampaignAction(action) {
    if (!currentContactData) {
        console.error('No current contact data for action:', action);
        return;
    }
    
    const messageTextarea = document.getElementById('preview-message');
    const message = messageTextarea ? messageTextarea.value : currentContactData.message;
    const contactIndex = currentContactData.contact_index;
    
    console.log('Handling campaign action:', action, 'for contact:', currentContactData.contact.Name);
    
    // Mark action as taken
    currentContactData.actionTaken = true;
    
    // Disable buttons and show loading
    const confirmSendBtn = document.getElementById('confirm-send');
    const confirmSkipBtn = document.getElementById('confirm-skip');
    
    if (confirmSendBtn) {
        confirmSendBtn.disabled = true;
        confirmSendBtn.innerHTML = action === 'send' ? 
            '<i class="fas fa-spinner fa-spin me-2"></i>Sending...' :
            '<i class="fas fa-paper-plane me-2"></i>Approve & Send';
    }
    if (confirmSkipBtn) {
        confirmSkipBtn.disabled = true;
        confirmSkipBtn.innerHTML = action === 'skip' ? 
            '<i class="fas fa-spinner fa-spin me-2"></i>Skipping...' :
            '<i class="fas fa-forward me-2"></i>Skip Contact';
    }
    
    // Send action to backend
    fetch('/campaign_action', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({
            campaign_id: campaignId,
            action: action,
            message: message,
            contact_index: parseInt(contactIndex)
        })
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            console.log('✅ Action sent successfully:', action);
            
            const actionMessages = {
                'send': `📤 Message approved and sent to ${currentContactData.contact.Name}!`,
                'skip': `⏭️ Skipped ${currentContactData.contact.Name}`
            };
            
            showAlert(actionMessages[action] || `Action "${action}" completed`, 'success');
            
            // Hide modal
            if (previewModal) {
                previewModal.hide();
            }
            
            // Clear current contact data
            currentContactData = null;
            
        } else {
            console.error('❌ Failed to send action:', data.error);
            showAlert(data.error || 'Failed to send action', 'error');
            
            // Mark action as not taken so user can retry
            if (currentContactData) {
                currentContactData.actionTaken = false;
            }
        }
    })
    .catch(error => {
        console.error('❌ Error sending campaign action:', error);
        showAlert('Network error: ' + error.message, 'error');
        
        // Mark action as not taken so user can retry
        if (currentContactData) {
            currentContactData.actionTaken = false;
        }
    })
    .finally(() => {
        // Re-enable buttons with original text
        if (confirmSendBtn) {
            confirmSendBtn.disabled = false;
            confirmSendBtn.innerHTML = '<i class="fas fa-paper-plane me-2"></i>Approve & Send';
        }
        if (confirmSkipBtn) {
            confirmSkipBtn.disabled = false;
            confirmSkipBtn.innerHTML = '<i class="fas fa-forward me-2"></i>Skip Contact';
        }
    });
}

function updateMessageLength() {
    const messageTextarea = document.getElementById('preview-message');
    const lengthDisplay = document.getElementById('message-length');
    
    if (messageTextarea && lengthDisplay) {
        const length = messageTextarea.value.length;
        lengthDisplay.textContent = length;
        
        // Change color based on length with smooth transitions
        if (length > 280) {
            lengthDisplay.style.color = '#dc3545'; // Red - over limit
            lengthDisplay.classList.add('fw-bold');
        } else if (length > 250) {
            lengthDisplay.style.color = '#fd7e14'; // Orange - near limit
            lengthDisplay.classList.add('fw-bold');
        } else if (length > 200) {
            lengthDisplay.style.color = '#ffc107'; // Yellow - getting close
            lengthDisplay.classList.remove('fw-bold');
        } else {
            lengthDisplay.style.color = '#6c757d'; // Gray - good
            lengthDisplay.classList.remove('fw-bold');
        }
    }
}

// Helper functions
function updateCounterAnimated(elementId, newValue) {
    const element = document.getElementById(elementId);
    if (!element) return;
    
    const currentValue = parseInt(element.textContent) || 0;
    
    if (currentValue !== newValue) {
        // Simple animation for counter updates
        element.style.transition = 'all 0.3s ease';
        element.style.transform = 'scale(1.1)';
        
        setTimeout(() => {
            element.textContent = newValue;
            element.style.transform = 'scale(1)';
        }, 150);
    }
}

function updateElement(elementId, value) {
    const element = document.getElementById(elementId);
    if (element) {
        element.textContent = value;
    }
}

function getStatusColor(status) {
    const colors = {
        'completed': 'success',
        'running': 'primary', 
        'processing': 'info',
        'failed': 'danger',
        'stopped': 'warning',
        'paused': 'secondary'
    };
    return colors[status.toLowerCase()] || 'secondary';
}

function showAlert(message, type = 'info') {
    // Remove existing alerts of the same type to prevent spam
    const existingAlerts = document.querySelectorAll(`.alert-${type}`);
    existingAlerts.forEach(alert => {
        if (alert.textContent.includes(message.substring(0, 20))) {
            alert.remove();
        }
    });
    
    // Create alert element with better styling
    const alertDiv = document.createElement('div');
    alertDiv.className = `alert alert-${type} alert-dismissible fade show shadow-sm`;
    alertDiv.innerHTML = `
        <div class="d-flex align-items-center">
            <div class="me-3">
                ${getAlertIcon(type)}
            </div>
            <div class="flex-grow-1">${message}</div>
            <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
        </div>
    `;
    
    // Insert at top of container
    const container = document.querySelector('.container');
    if (container && container.firstChild) {
        container.insertBefore(alertDiv, container.firstChild);
    }
    
    // Auto-dismiss after 4 seconds (except for errors)
    if (type !== 'danger') {
        setTimeout(() => {
            if (alertDiv.parentNode) {
                alertDiv.remove();
            }
        }, 4000);
    }
}

function getAlertIcon(type) {
    const icons = {
        'success': '<i class="fas fa-check-circle"></i>',
        'error': '<i class="fas fa-exclamation-triangle"></i>',
        'danger': '<i class="fas fa-exclamation-triangle"></i>',
        'warning': '<i class="fas fa-exclamation-circle"></i>',
        'info': '<i class="fas fa-info-circle"></i>'
    };
    return icons[type] || icons['info'];
}