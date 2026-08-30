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

    var status = form.querySelector('#osw-status');
    var button = form.querySelector('#osw-run');
    var fileInput = form.querySelector('#osw-file-input');
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

    if (fileInput) {
      fileInput.addEventListener('change', function() {
        resetToReady();
      });
    }

    // ── Rate plan toggle & labels ───────────────────────────────────────────
    var rateEnabledCb = form.querySelector('#osw-rate-enabled');
    var rateToggle    = form.querySelector('#osw-rate-toggle');
    var rateKnob      = form.querySelector('#osw-rate-knob');
    var rateFields    = form.querySelector('#osw-rate-fields');
    var rateUnitSel   = form.querySelector('#osw-rate-unit');
    var minLabel      = form.querySelector('#osw-min-label');
    var maxLabel      = form.querySelector('#osw-max-label');

    function updateRateToggle() {
      var on = !!(rateEnabledCb && rateEnabledCb.checked);
      if (rateToggle) {
        rateToggle.style.background = on ? '#22c55e' : 'rgba(255,255,255,0.1)';
        rateToggle.style.borderColor = on ? '#16a34a' : 'rgba(255,255,255,0.25)';
      }
      if (rateKnob) {
        rateKnob.style.left = on ? '20px' : '2px';
      }
      if (rateFields) {
        rateFields.style.display = on ? 'block' : 'none';
        rateFields.setAttribute('aria-hidden', on ? 'false' : 'true');
      }
    }

    function updateRateUnitLabels() {
      var unit = rateUnitSel ? rateUnitSel.value : 'KG_HA';
      if (minLabel) minLabel.textContent = 'Min rate (' + unit + ')';
      if (maxLabel) maxLabel.textContent = 'Max rate (' + unit + ')';
    }

    if (rateEnabledCb) rateEnabledCb.addEventListener('change', updateRateToggle);
    if (rateUnitSel) rateUnitSel.addEventListener('change', updateRateUnitLabels);

    updateRateToggle();
    updateRateUnitLabels();

    // ── Spot Spray Rate plan toggle & labels ────────────────────────────────
    var spotRateEnabledCb = form.querySelector('#osw-spot-rate-enabled');
    var spotRateToggle    = form.querySelector('#osw-spot-rate-toggle');
    var spotRateKnob      = form.querySelector('#osw-spot-rate-knob');
    var spotRateFields    = form.querySelector('#osw-spot-rate-fields');
    var spotRateUnitSel   = form.querySelector('#osw-spot-rate-unit');
    var spotTargetLabel   = form.querySelector('#osw-spot-target-label');

    function updateSpotRateToggle() {
      var on = !!(spotRateEnabledCb && spotRateEnabledCb.checked);
      if (spotRateToggle) {
        spotRateToggle.style.background = on ? '#22c55e' : 'rgba(255,255,255,0.1)';
        spotRateToggle.style.borderColor = on ? '#16a34a' : 'rgba(255,255,255,0.25)';
      }
      if (spotRateKnob) {
        spotRateKnob.style.left = on ? '20px' : '2px';
      }
      if (spotRateFields) {
        spotRateFields.style.display = on ? 'block' : 'none';
        spotRateFields.setAttribute('aria-hidden', on ? 'false' : 'true');
      }
    }

    function updateSpotRateUnitLabels() {
      var unit = spotRateUnitSel ? spotRateUnitSel.value : 'L_HA';
      if (spotTargetLabel) spotTargetLabel.textContent = 'Target application rate (' + unit + ')';
    }

    if (spotRateEnabledCb) spotRateEnabledCb.addEventListener('change', updateSpotRateToggle);
    if (spotRateUnitSel) spotRateUnitSel.addEventListener('change', updateSpotRateUnitLabels);

    updateSpotRateToggle();
    updateSpotRateUnitLabels();
    resetToReady();

    async function readJson(response, context) {
      var text = await response.text();
      try {
        return text ? JSON.parse(text) : {};
      } catch (error) {
        throw new Error(context + ' returned an invalid response (HTTP ' + response.status + ').');
      }
    }

    // ── Polling Celery worker ───────────────────────────────────────────────
    async function pollWorker(id) {
      for (;;) {
        var response = await fetch('/api/workers/check/' + encodeURIComponent(id), { credentials: 'same-origin' });
        var result = await readJson(response, 'Worker status');
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
          var productName = (form.querySelector('#osw-product-name') || {}).value || '';
          var rateUnit    = rateUnitSel ? rateUnitSel.value : 'KG_HA';
          var strategy    = (form.querySelector('#osw-rate-strategy') || {}).value || 'direct';
          var rateBasis   = (form.querySelector('#osw-rate-basis') || {}).value || 'product';
          var minRate     = Number((form.querySelector('#osw-min-rate') || {}).value);
          var maxRate     = Number((form.querySelector('#osw-max-rate') || {}).value);
          var approvedBy  = ((form.querySelector('#osw-approved-by') || {}).value || '').trim();
          if (!Number.isFinite(minRate) || minRate < 0 || !Number.isFinite(maxRate) || maxRate <= 0) {
            throw new Error('Enter valid fertilizer rates; maximum rate must be greater than zero.');
          }
          if (maxRate < minRate) throw new Error('Maximum fertilizer rate cannot be lower than minimum rate.');
          if (!approvedBy) throw new Error('Enter the person who approved the fertilizer rate plan.');
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
          var spotProductName = (form.querySelector('#osw-spot-product-name') || {}).value || '';
          var spotRateUnit    = spotRateUnitSel ? spotRateUnitSel.value : 'L_HA';
          var spotTargetRate  = Number((form.querySelector('#osw-spot-target-rate') || {}).value);
          var spotApprovedBy  = ((form.querySelector('#osw-spot-approved-by') || {}).value || '').trim();
          if (!Number.isFinite(spotTargetRate) || spotTargetRate <= 0) {
            throw new Error('Enter a spot-spray target rate greater than zero.');
          }
          if (!spotApprovedBy) throw new Error('Enter the person who approved the spot-spray rate plan.');
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

        var result = await readJson(response, 'Analysis request');
        if (!response.ok || result.error) {
          throw new Error(result.error || ('Request HTTP ' + response.status));
        }

        if (!result.celery_task_id) throw new Error('The server did not return a worker task ID.');
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