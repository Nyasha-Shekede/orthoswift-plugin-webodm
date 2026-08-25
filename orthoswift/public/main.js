/* ─── OrthoSWIFT WebODM Plugin Frontend Controller ────────────────────────── */
(function() {
  function getCookie(name) {
    var row = document.cookie.split('; ').find(function(x) { return x.startsWith(name + '='); });
    return row ? decodeURIComponent(row.split('=').slice(1).join('=')) : '';
  }

  function setStatus(node, kind, text) {
    if (!node) return;
    if (!text) {
      node.style.display = 'none';
      node.className = 'osw-status';
      node.textContent = '';
      return;
    }
    node.className = 'osw-status ' + kind;
    node.textContent = text;
    node.style.display = 'inline-block';
  }

  function initOrthoSWIFT() {
    var form = document.querySelector('#osw-form');
    if (!form) return;

    // Prevent duplicate initialization if script runs twice
    if (form.dataset.oswInitialized) return;
    form.dataset.oswInitialized = 'true';

    var status = document.querySelector('#osw-status');
    var button = document.querySelector('#osw-run');
    var fileInput = document.querySelector('#osw-file-input');
    var projInput = form.querySelector('[name=project_id]');
    var taskInput = form.querySelector('[name=task_id]');
    var latestDownloadUrl = null;

    function resetToReady() {
      latestDownloadUrl = null;
      if (button) {
        button.disabled = false;
        button.className = 'state-ready';
        button.textContent = 'Run analysis';
      }
      setStatus(status, '', '');
    }

    function updateVisibility() {
      var proj = (projInput && projInput.value ? projInput.value : '').trim();
      var task = (taskInput && taskInput.value ? taskInput.value : '').trim();
      var hasFile = !!(fileInput && fileInput.files && fileInput.files.length > 0);
      var hasSelection = hasFile || (proj !== '' && task !== '');

      if (button) {
        button.style.display = hasSelection ? 'inline-block' : 'none';
        if (hasSelection && !button.classList.contains('state-running') && !button.classList.contains('state-success')) {
          resetToReady();
        }
      }
    }

    if (fileInput) {
      fileInput.addEventListener('change', function() {
        resetToReady();
        updateVisibility();
      });
    }

    form.addEventListener('input', updateVisibility);

    // ── Rate plan toggle & labels ───────────────────────────────────────────
    var rateEnabledCb = document.querySelector('#osw-rate-enabled');
    var rateToggle    = document.querySelector('#osw-rate-toggle');
    var rateKnob      = document.querySelector('#osw-rate-knob');
    var rateFields    = document.querySelector('#osw-rate-fields');
    var rateUnitSel   = document.querySelector('#osw-rate-unit');
    var minLabel      = document.querySelector('#osw-min-label');
    var maxLabel      = document.querySelector('#osw-max-label');

    function updateRateToggle() {
      var on = rateEnabledCb && rateEnabledCb.checked;
      if (rateToggle) {
        rateToggle.style.background = on ? '#22c55e' : 'rgba(255,255,255,0.1)';
        rateToggle.style.borderColor = on ? '#16a34a' : 'rgba(255,255,255,0.25)';
      }
      if (rateKnob) {
        rateKnob.style.left = on ? '20px' : '2px';
      }
      if (rateFields) {
        rateFields.style.display = on ? 'block' : 'none';
      }
    }

    function updateRateUnitLabels() {
      var unit = rateUnitSel ? rateUnitSel.value : 'KG_HA';
      if (minLabel) minLabel.textContent = 'Min rate (' + unit + ')';
      if (maxLabel) maxLabel.textContent = 'Max rate (' + unit + ')';
    }

    if (rateEnabledCb) rateEnabledCb.addEventListener('change', updateRateToggle);
    if (rateUnitSel) rateUnitSel.addEventListener('change', updateRateUnitLabels);
    if (rateToggle && rateToggle.parentElement) {
      rateToggle.parentElement.addEventListener('click', function(e) {
        if (e.target !== rateEnabledCb && rateEnabledCb) {
          rateEnabledCb.checked = !rateEnabledCb.checked;
          updateRateToggle();
        }
      });
    }

    updateRateToggle();

    // ── Spot Spray Rate plan toggle & labels ────────────────────────────────
    var spotRateEnabledCb = document.querySelector('#osw-spot-rate-enabled');
    var spotRateToggle    = document.querySelector('#osw-spot-rate-toggle');
    var spotRateKnob      = document.querySelector('#osw-spot-rate-knob');
    var spotRateFields    = document.querySelector('#osw-spot-rate-fields');
    var spotRateUnitSel   = document.querySelector('#osw-spot-rate-unit');
    var spotTargetLabel   = document.querySelector('#osw-spot-target-label');

    function updateSpotRateToggle() {
      var on = spotRateEnabledCb && spotRateEnabledCb.checked;
      if (spotRateToggle) {
        spotRateToggle.style.background = on ? '#22c55e' : 'rgba(255,255,255,0.1)';
        spotRateToggle.style.borderColor = on ? '#16a34a' : 'rgba(255,255,255,0.25)';
      }
      if (spotRateKnob) {
        spotRateKnob.style.left = on ? '20px' : '2px';
      }
      if (spotRateFields) {
        spotRateFields.style.display = on ? 'block' : 'none';
      }
    }

    function updateSpotRateUnitLabels() {
      var unit = spotRateUnitSel ? spotRateUnitSel.value : 'L_HA';
      if (spotTargetLabel) spotTargetLabel.textContent = 'Target application rate (' + unit + ')';
    }

    if (spotRateEnabledCb) spotRateEnabledCb.addEventListener('change', updateSpotRateToggle);
    if (spotRateUnitSel) spotRateUnitSel.addEventListener('change', updateSpotRateUnitLabels);
    if (spotRateToggle && spotRateToggle.parentElement) {
      spotRateToggle.parentElement.addEventListener('click', function(e) {
        if (e.target !== spotRateEnabledCb && spotRateEnabledCb) {
          spotRateEnabledCb.checked = !spotRateEnabledCb.checked;
          updateSpotRateToggle();
        }
      });
    }

    updateSpotRateToggle();
    updateVisibility();

    // ── Polling Celery worker ───────────────────────────────────────────────
    async function pollWorker(id) {
      for (;;) {
        var response = await fetch('/api/workers/check/' + encodeURIComponent(id), { credentials: 'same-origin' });
        var result = await response.json();
        if (!response.ok) throw new Error(result.error || ('Worker status HTTP ' + response.status));
        if (result.error) throw new Error(result.error);
        if (result.canceled) throw new Error('Analysis was canceled.');
        if (result.ready) return;

        var label = (result.status || 'Processing') + (result.progress !== undefined ? ' · ' + result.progress + '%' : '');
        if (button) button.textContent = label;
        await new Promise(function(resolve) { setTimeout(resolve, 1500); });
      }
    }

    function triggerDownload(url) {
      var link = document.createElement('a');
      link.href = url;
      link.download = 'orthoswift-deliverables.zip';
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
    }

    // ── Form Submission ─────────────────────────────────────────────────────
    form.addEventListener('submit', async function(event) {
      event.preventDefault();

      if (button && button.classList.contains('state-success') && latestDownloadUrl) {
        triggerDownload(latestDownloadUrl);
        return;
      }

      var file = fileInput && fileInput.files && fileInput.files[0];
      var proj = (projInput && projInput.value ? projInput.value : '').trim();
      var task = (taskInput && taskInput.value ? taskInput.value : '').trim();

      if (!file && (!proj || !task)) {
        setStatus(status, 'error', 'Please select a local orthomosaic file (.tif) or choose a WebODM task.');
        return;
      }

      setStatus(status, '', '');
      if (button) {
        button.disabled = true;
        button.className = 'state-running';
        button.textContent = 'Uploading orthomosaic…';
      }

      try {
        var ratePlan = null;
        var rateOn = rateEnabledCb && rateEnabledCb.checked;
        if (rateOn) {
          var productName = (document.querySelector('#osw-product-name') || {}).value || '';
          var rateUnit    = rateUnitSel ? rateUnitSel.value : 'KG_HA';
          var strategy    = (document.querySelector('#osw-rate-strategy') || {}).value || 'direct';
          var rateBasis   = (document.querySelector('#osw-rate-basis') || {}).value || 'product';
          var minRate     = parseFloat((document.querySelector('#osw-min-rate') || {}).value) || 0;
          var maxRate     = parseFloat((document.querySelector('#osw-max-rate') || {}).value) || 0;
          var approvedBy  = (document.querySelector('#osw-approved-by') || {}).value || 'Operator';
          ratePlan = {
            mode: 'physical',
            operation: 'fertilizer',
            product_name: productName || 'Fertilizer',
            rate_basis: rateBasis,
            unit: rateUnit,
            strategy: strategy,
            min_rate: minRate,
            max_rate: maxRate,
            approved_by: approvedBy
          };
        }

        var spotRatePlan = null;
        var spotRateOn = spotRateEnabledCb && spotRateEnabledCb.checked;
        if (spotRateOn) {
          var spotProductName = (document.querySelector('#osw-spot-product-name') || {}).value || '';
          var spotRateUnit    = spotRateUnitSel ? spotRateUnitSel.value : 'L_HA';
          var spotTargetRate  = parseFloat((document.querySelector('#osw-spot-target-rate') || {}).value) || 0;
          var spotApprovedBy  = (document.querySelector('#osw-spot-approved-by') || {}).value || 'Operator';
          spotRatePlan = {
            mode: 'physical',
            operation: 'spray',
            product_name: spotProductName || 'Herbicide',
            rate_basis: 'product',
            unit: spotRateUnit,
            strategy: 'target_hotspots',
            min_rate: spotTargetRate,
            max_rate: spotTargetRate,
            approved_by: spotApprovedBy
          };
        }

        var response;
        if (file) {
          var uploadData = new FormData();
          uploadData.append('orthomosaic_file', file);
          uploadData.append('zones', 3);
          uploadData.append('offline_basemap', 'true');
          if (ratePlan) {
            uploadData.append('fertilizer_rate_plan', JSON.stringify(ratePlan));
          }
          if (spotRatePlan) {
            uploadData.append('spot_spray_rate_plan', JSON.stringify(spotRatePlan));
          }

          response = await fetch('/api/plugins/orthoswift/run', {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'X-CSRFToken': getCookie('csrftoken') },
            body: uploadData
          });
        } else {
          var body = {
            project_id: proj,
            task_id: task,
            zones: 3,
            offline_basemap: true,
            fertilizer_rate_plan: ratePlan,
            spot_spray_rate_plan: spotRatePlan
          };
          response = await fetch('/api/plugins/orthoswift/run', {
            method: 'POST',
            credentials: 'same-origin',
            headers: {
              'Content-Type': 'application/json',
              'X-CSRFToken': getCookie('csrftoken')
            },
            body: JSON.stringify(body)
          });
        }

        var result = await response.json();
        if (!response.ok || result.error) {
          throw new Error(result.error || ('Request HTTP ' + response.status));
        }

        if (button) button.textContent = 'Starting worker…';
        await pollWorker(result.celery_task_id);

        latestDownloadUrl = '/api/workers/get/' + encodeURIComponent(result.celery_task_id) + '?filename=orthoswift-deliverables.zip';
        triggerDownload(latestDownloadUrl);

        if (button) {
          button.disabled = false;
          button.className = 'state-success';
          button.textContent = 'Download Deliverables (.zip)';
        }
      } catch (error) {
        if (button) {
          button.disabled = false;
          button.className = 'state-error';
          button.textContent = 'Retry Analysis';
        }
        setStatus(status, 'error', 'ERROR: ' + error.message);
      }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initOrthoSWIFT);
  } else {
    initOrthoSWIFT();
  }
})();