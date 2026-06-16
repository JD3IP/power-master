/**
 * TOU Tariff Editor - Visualiser-first, click-to-edit frontend
 * Phase 5 Units U5-2 + U5-3: Template + Interactive JS/CSS
 */

(function() {
  'use strict';

  let plan = null;
  let savedPlan = null;
  let slots = null;
  const RESOLVE_DEBOUNCE_MS = 300;
  let resolveTimer = null;

  /**
   * Initialize the editor on DOM load
   */
  document.addEventListener('DOMContentLoaded', initEditor);

  function initEditor() {
    const editorHost = document.getElementById('tou-editor');
    if (!editorHost) return;

    // Parse embedded plan data
    const planDataEl = document.getElementById('tou-plan-data');
    if (!planDataEl) return;

    try {
      const tariffConfig = JSON.parse(planDataEl.textContent);
      plan = tariffConfig;
      savedPlan = JSON.parse(JSON.stringify(plan));
    } catch (e) {
      console.error('Failed to parse tariff config:', e);
      return;
    }

    // Store canEdit state globally for later reference
    window.touEditorCanEdit = editorHost.getAttribute('data-can-edit') !== 'false';

    // Render the editor UI
    renderEditor(editorHost);

    // Initial resolve + render ribbon
    debounceResolve();

    // Wire up event handlers for provider type switching
    wireProviderTypeButtons();
  }

  /**
   * Wire up Amber/TOU type selector buttons (if they exist on the settings page)
   */
  function wireProviderTypeButtons() {
    const amberBtn = document.getElementById('tariff-type-amber');
    const touBtn = document.getElementById('tariff-type-tou');

    if (amberBtn) {
      amberBtn.addEventListener('click', (e) => {
        e.preventDefault();
        switchTariffTypeBranch('amber');
      });
    }
    if (touBtn) {
      touBtn.addEventListener('click', (e) => {
        e.preventDefault();
        switchTariffTypeBranch('tou');
      });
    }
  }

  /**
   * Show/hide Amber/TOU branches
   */
  window.switchTariffType = function(type) {
    switchTariffTypeBranch(type);
  };

  function switchTariffTypeBranch(type) {
    const amberBranch = document.getElementById('tariff-amber-branch');
    const touBranch = document.getElementById('tariff-tou-branch');
    const amberBtn = document.getElementById('tariff-type-amber');
    const touBtn = document.getElementById('tariff-type-tou');

    if (type === 'amber') {
      if (amberBranch) amberBranch.style.display = 'block';
      if (touBranch) touBranch.style.display = 'none';
      if (amberBtn) amberBtn.classList.remove('btn-secondary');
      if (amberBtn) amberBtn.classList.add('btn-primary');
      if (touBtn) touBtn.classList.add('btn-secondary');
      if (touBtn) touBtn.classList.remove('btn-primary');
    } else {
      if (amberBranch) amberBranch.style.display = 'none';
      if (touBranch) touBranch.style.display = 'block';
      if (touBtn) touBtn.classList.remove('btn-secondary');
      if (touBtn) touBtn.classList.add('btn-primary');
      if (amberBtn) amberBtn.classList.add('btn-secondary');
      if (amberBtn) amberBtn.classList.remove('btn-primary');
    }
  }

  /**
   * Main editor HTML structure
   */
  function renderEditor(host) {
    // Check if editor is read-only
    const canEdit = host.getAttribute('data-can-edit') !== 'false';

    const html = `
      <div class="tou-editor-wrapper">
        <!-- Ribbon Hero + Cost Readout -->
        <div class="dash-panel">
          <div class="dash-panel-title">24-Hour Tariff Preview ${canEdit ? '(Click to edit)' : '(Read Only)'}</div>
          <div class="dash-panel-body">
            <div style="position: relative;">
              <div id="tou-ribbon-status" style="font-size: 12px; color: var(--text-muted); margin-bottom: 8px;">Loading...</div>
              <div class="ribbon-labels" id="tou-ribbon-labels" style="display: flex; justify-content: space-between; font-size: 11px; color: var(--text-muted); padding: 0 2px; margin-bottom: 4px;">
                <!-- Populated by renderRibbon -->
              </div>
              <div id="tou-ribbon-main" class="ribbon" style="height: 60px; display: flex; border: 1px solid var(--border); border-radius: 6px; overflow: hidden; margin-bottom: 3px;">
                <!-- Populated by renderRibbon -->
              </div>
              <div id="tou-ribbon-feedin" class="ribbon-sub" style="height: 20px; display: flex; border: 1px solid var(--border); border-top: none; border-radius: 0 0 6px 6px; background: var(--bg-secondary); overflow: hidden;">
                <!-- Populated by renderRibbon -->
              </div>
              <div id="tou-ribbon-ev" class="ribbon-sub" style="height: 12px; display: flex; border: 1px solid var(--border); border-top: none; border-radius: 0 0 6px 6px; background: var(--bg-secondary); overflow: hidden; margin-top: 1px; margin-bottom: 8px;">
                <!-- Populated by renderRibbon if EV windows exist -->
              </div>
              <div id="tou-cost-readout" style="font-size: 13px; color: var(--text-secondary); margin-top: 8px;">
                <!-- Populated after slots resolve -->
              </div>
              <div id="tou-gap-badge" style="display: none; margin-top: 8px; padding: 8px 12px; background: rgba(248, 81, 73, 0.15); border: 1px solid var(--accent-red); border-radius: 4px; color: var(--accent-red); font-size: 12px; font-weight: 500;">
                <!-- Error message if uncovered hours detected -->
              </div>
            </div>
          </div>
        </div>

        <!-- Plan-level settings -->
        <div class="dash-panel">
          <div class="dash-panel-title">Plan Details</div>
          <div class="dash-panel-body">
            <div class="form-group">
              <label for="tou-supply-charge" class="form-label">Supply Charge (¢/day)</label>
              <input type="number" id="tou-supply-charge" class="form-input" step="0.01" style="width: 100%;">
            </div>
            <div class="form-group">
              <label for="tou-timezone" class="form-label">Timezone</label>
              <input type="text" id="tou-timezone" class="form-input" placeholder="e.g., Australia/Brisbane" style="width: 100%;">
            </div>
            <div class="form-group">
              <label for="tou-grid-charge-policy" class="form-label">Grid Charge Policy</label>
              <select id="tou-grid-charge-policy" class="form-input" style="width: 100%;">
                <option value="free_window_and_solar_only">Free Window + Solar Only</option>
                <option value="always_available">Always Available</option>
              </select>
            </div>
            <div class="form-group">
              <label for="tou-billing-length" class="form-label">Billing Cycle Length (days)</label>
              <input type="number" id="tou-billing-length" class="form-input" min="1" style="width: 100%;">
            </div>
            <div class="form-group">
              <label for="tou-billing-anchor" class="form-label">Billing Anchor Date (YYYY-MM-DD)</label>
              <input type="text" id="tou-billing-anchor" class="form-input" style="width: 100%;">
            </div>
            <div class="form-group">
              <label for="tou-vpp-enabled" style="display: flex; align-items: center; gap: 8px; cursor: pointer;">
                <input type="checkbox" id="tou-vpp-enabled" style="width: auto;">
                <span>VPP Enrolled</span>
              </label>
            </div>
            <div class="form-group">
              <label for="tou-version-from" class="form-label">Version Valid From (YYYY-MM-DD)</label>
              <input type="text" id="tou-version-from" class="form-input" style="width: 100%;">
            </div>
            <div class="form-group">
              <label for="tou-version-until" class="form-label">Version Valid Until (leave blank for open-ended)</label>
              <input type="text" id="tou-version-until" class="form-input" style="width: 100%;">
            </div>
          </div>
        </div>

        <!-- Import Bands -->
        <div class="dash-panel">
          <div class="dash-panel-title">Import Bands</div>
          <div class="dash-panel-body" id="tou-import-bands-list">
            <!-- Populated by renderImportBands -->
          </div>
          <div style="padding: 12px; border-top: 1px solid var(--border);">
            <button type="button" class="btn btn-secondary" style="width: 100%;" onclick="window.touEditor.addImportBand()">+ Add Import Band</button>
          </div>
        </div>

        <!-- Free Windows -->
        <div class="dash-panel">
          <div class="dash-panel-title">Free Windows</div>
          <div class="dash-panel-body" id="tou-free-windows-list">
            <!-- Populated by renderFreeWindows -->
          </div>
          <div style="padding: 12px; border-top: 1px solid var(--border);">
            <button type="button" class="btn btn-secondary" style="width: 100%;" onclick="window.touEditor.addFreeWindow()">+ Add Free Window</button>
          </div>
        </div>

        <!-- Feed-in Bands -->
        <div class="dash-panel">
          <div class="dash-panel-title">Feed-in Bands</div>
          <div class="dash-panel-body" id="tou-feedin-bands-list">
            <!-- Populated by renderFeedInBands -->
          </div>
          <div style="padding: 12px; border-top: 1px solid var(--border);">
            <button type="button" class="btn btn-secondary" style="width: 100%;" onclick="window.touEditor.addFeedInBand()">+ Add Feed-in Band</button>
          </div>
        </div>

        <!-- Credits (empty state) -->
        <div class="dash-panel">
          <div class="dash-panel-title">Credits</div>
          <div class="dash-panel-body" id="tou-credits-list">
            <p style="color: var(--text-muted); font-size: 12px; margin: 0;">No credits configured. Add credits for bulk incentive programs.</p>
          </div>
          <div style="padding: 12px; border-top: 1px solid var(--border);">
            <button type="button" class="btn btn-secondary" style="width: 100%; opacity: 0.5; cursor: not-allowed;" disabled>+ Add Credit</button>
          </div>
        </div>

        <!-- Save/Export bar -->
        <div style="padding: 12px; background: var(--bg-secondary); border-top: 1px solid var(--border); margin-top: 24px; display: flex; justify-content: space-between; align-items: center; border-radius: 0 0 6px 6px; gap: 8px;">
          <span id="tou-save-status" style="font-size: 12px; color: var(--text-muted);">Unsaved changes</span>
          <div style="display: flex; gap: 8px;">
            ${canEdit ? `<button type="button" id="tou-wizard-btn" class="btn btn-secondary" onclick="window.touEditor.openWizardModal()" title="Build from Energy Fact Sheet">⚙ Build from EFS</button>` : ''}
            <button type="button" id="tou-export-btn" class="btn btn-secondary" onclick="window.touEditor.exportPlan()" title="Download plan as JSON">↓ Export</button>
            ${canEdit ? `<button type="button" id="tou-save-btn" class="btn btn-primary" onclick="window.touEditor.openSaveDialog()">Save Plan</button>` : ''}
          </div>
        </div>
      </div>

      <!-- Diff Preview Modal -->
      <div id="tou-save-modal" style="display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.5); z-index: 2000; align-items: center; justify-content: center;">
        <div style="background: var(--bg-card); border: 1px solid var(--border); border-radius: 6px; padding: 24px; max-width: 600px; width: 90%; max-height: 80vh; overflow-y: auto;">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px;">
            <h3 style="margin: 0; font-size: 16px; font-weight: 600;">Review Changes</h3>
            <button type="button" style="background: none; border: none; color: var(--text-secondary); cursor: pointer; font-size: 20px;" onclick="window.touEditor.closeSaveDialog()">×</button>
          </div>
          <div id="tou-diff-list" style="margin-bottom: 16px; font-size: 13px;">
            <!-- Populated by openSaveDialog -->
          </div>
          <div style="display: flex; gap: 8px; justify-content: flex-end;">
            <button type="button" class="btn btn-secondary" onclick="window.touEditor.closeSaveDialog()">Cancel</button>
            <button type="button" id="tou-confirm-save-btn" class="btn btn-primary" onclick="window.touEditor.doSave()">Confirm & Save</button>
          </div>
          <div id="tou-save-feedback" style="margin-top: 12px; font-size: 12px; color: var(--text-muted);"></div>
        </div>
      </div>

      <!-- Popover for band editing (inline) -->
      <div id="tou-band-popover" style="display: none; position: absolute; background: var(--bg-card); border: 1px solid var(--border); border-radius: 6px; padding: 12px; min-width: 250px; z-index: 1500; box-shadow: 0 4px 16px rgba(0,0,0,0.4);">
        <div style="font-size: 12px; color: var(--text-muted); margin-bottom: 8px;" id="tou-popover-title"></div>
        <div id="tou-popover-content" style="margin-bottom: 8px;">
          <!-- Populated by openBandEditor -->
        </div>
        <div style="display: flex; gap: 6px;">
          <button type="button" class="btn btn-sm" onclick="window.touEditor.closeBandPopover()">Close</button>
        </div>
      </div>

      <!-- EFS Wizard Modal -->
      <div id="tou-wizard-modal" style="display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.5); z-index: 2000; align-items: center; justify-content: center;">
        <div style="background: var(--bg-card); border: 1px solid var(--border); border-radius: 6px; padding: 24px; max-width: 700px; width: 90%; max-height: 90vh; overflow-y: auto;">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px;">
            <h3 style="margin: 0; font-size: 16px; font-weight: 600;">Build from Energy Fact Sheet</h3>
            <button type="button" style="background: none; border: none; color: var(--text-secondary); cursor: pointer; font-size: 20px;" onclick="window.touEditor.closeWizardModal()">×</button>
          </div>

          <!-- Template Selector -->
          <div style="margin-bottom: 20px; padding: 12px; background: var(--bg-secondary); border-radius: 6px;">
            <label style="font-size: 12px; color: var(--text-muted); display: block; margin-bottom: 8px; text-transform: uppercase; font-weight: 500;">Start from known plan (optional)</label>
            <select id="tou-wizard-template-select" style="width: 100%; padding: 8px; background: var(--bg-primary); border: 1px solid var(--border); border-radius: 4px; color: var(--text-primary);" onchange="window.touEditor.wizardLoadTemplate()">
              <option value="">-- Enter manually --</option>
            </select>
          </div>

          <!-- Wizard Form -->
          <form id="tou-wizard-form" style="display: flex; flex-direction: column; gap: 16px;">
            <!-- Plan Basics -->
            <fieldset style="border: 1px solid var(--border); border-radius: 6px; padding: 12px;">
              <legend style="font-weight: 600; padding: 0 8px;">Plan Basics</legend>
              <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px;">
                <div class="form-group" style="margin: 0;">
                  <label style="font-size: 11px; color: var(--text-muted); display: block; margin-bottom: 4px;">Supply Charge (¢/day)</label>
                  <input type="number" id="wizard-supply-charge" step="0.01" style="width: 100%; padding: 6px; font-size: 12px; background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 4px; color: var(--text-primary);" value="148.5">
                </div>
                <div class="form-group" style="margin: 0;">
                  <label style="font-size: 11px; color: var(--text-muted); display: block; margin-bottom: 4px;">Timezone</label>
                  <input type="text" id="wizard-timezone" style="width: 100%; padding: 6px; font-size: 12px; background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 4px; color: var(--text-primary);" value="Australia/Brisbane">
                </div>
              </div>
              <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 8px;">
                <div class="form-group" style="margin: 0;">
                  <label style="font-size: 11px; color: var(--text-muted); display: block; margin-bottom: 4px;">Billing Length (days)</label>
                  <input type="number" id="wizard-billing-length" min="1" style="width: 100%; padding: 6px; font-size: 12px; background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 4px; color: var(--text-primary);" value="28">
                </div>
                <div class="form-group" style="margin: 0;">
                  <label style="font-size: 11px; color: var(--text-muted); display: block; margin-bottom: 4px;">Anchor Date (YYYY-MM-DD)</label>
                  <input type="text" id="wizard-anchor-date" style="width: 100%; padding: 6px; font-size: 12px; background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 4px; color: var(--text-primary);" value="2026-06-01">
                </div>
              </div>
            </fieldset>

            <!-- Peak Band -->
            <fieldset style="border: 1px solid var(--border); border-radius: 6px; padding: 12px;">
              <legend style="font-weight: 600; padding: 0 8px;">Peak (Import)</legend>
              <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px;">
                <div class="form-group" style="margin: 0;">
                  <label style="font-size: 11px; color: var(--text-muted); display: block; margin-bottom: 4px;">Rate (¢/kWh)</label>
                  <input type="number" id="wizard-peak-rate" step="0.01" style="width: 100%; padding: 6px; font-size: 12px; background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 4px; color: var(--text-primary);" value="55.55">
                </div>
              </div>
              <div style="margin-top: 8px;">
                <label style="font-size: 11px; color: var(--text-muted); display: block; margin-bottom: 4px;">Windows (HH:MM-HH:MM, comma-separated)</label>
                <input type="text" id="wizard-peak-windows" style="width: 100%; padding: 6px; font-size: 12px; background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 4px; color: var(--text-primary);" value="16:00-22:59" placeholder="e.g. 16:00-22:59, 06:00-07:00">
              </div>
            </fieldset>

            <!-- Free/Off-Peak Window -->
            <fieldset style="border: 1px solid var(--border); border-radius: 6px; padding: 12px;">
              <legend style="font-weight: 600; padding: 0 8px;">Free / Off-Peak Window</legend>
              <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px;">
                <div class="form-group" style="margin: 0;">
                  <label style="font-size: 11px; color: var(--text-muted); display: block; margin-bottom: 4px;">Window Name</label>
                  <input type="text" id="wizard-free-name" style="width: 100%; padding: 6px; font-size: 12px; background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 4px; color: var(--text-primary);" value="free">
                </div>
                <div class="form-group" style="margin: 0;">
                  <label style="font-size: 11px; color: var(--text-muted); display: block; margin-bottom: 4px;">Daily Cap (kWh)</label>
                  <input type="number" id="wizard-free-cap" step="0.1" style="width: 100%; padding: 6px; font-size: 12px; background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 4px; color: var(--text-primary);" value="50">
                </div>
              </div>
              <div style="margin-top: 8px;">
                <label style="font-size: 11px; color: var(--text-muted); display: block; margin-bottom: 4px;">Windows (HH:MM-HH:MM, comma-separated)</label>
                <input type="text" id="wizard-free-windows" style="width: 100%; padding: 6px; font-size: 12px; background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 4px; color: var(--text-primary);" value="10:00-13:59" placeholder="e.g. 10:00-13:59">
              </div>
              <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 8px;">
                <div class="form-group" style="margin: 0;">
                  <label style="font-size: 11px; color: var(--text-muted); display: block; margin-bottom: 4px;">Off-Peak Rate (¢/kWh)</label>
                  <input type="number" id="wizard-offpeak-rate" step="0.01" style="width: 100%; padding: 6px; font-size: 12px; background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 4px; color: var(--text-primary);" value="28.6">
                </div>
              </div>
            </fieldset>

            <!-- Shoulder/Default -->
            <fieldset style="border: 1px solid var(--border); border-radius: 6px; padding: 12px;">
              <legend style="font-weight: 600; padding: 0 8px;">Shoulder / Default (Import)</legend>
              <div style="display: grid; grid-template-columns: 1fr; gap: 8px;">
                <div class="form-group" style="margin: 0;">
                  <label style="font-size: 11px; color: var(--text-muted); display: block; margin-bottom: 4px;">Rate (¢/kWh)</label>
                  <input type="number" id="wizard-shoulder-rate" step="0.01" style="width: 100%; padding: 6px; font-size: 12px; background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 4px; color: var(--text-primary);" value="34.1">
                </div>
              </div>
            </fieldset>

            <!-- Feed-In -->
            <fieldset style="border: 1px solid var(--border); border-radius: 6px; padding: 12px;">
              <legend style="font-weight: 600; padding: 0 8px;">Feed-In (Export)</legend>
              <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px;">
                <div class="form-group" style="margin: 0;">
                  <label style="font-size: 11px; color: var(--text-muted); display: block; margin-bottom: 4px;">FiT Rate (¢/kWh)</label>
                  <input type="number" id="wizard-fit-rate" step="0.01" style="width: 100%; padding: 6px; font-size: 12px; background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 4px; color: var(--text-primary);" value="8">
                </div>
              </div>
              <div style="margin-top: 8px;">
                <label style="font-size: 11px; color: var(--text-muted); display: block; margin-bottom: 4px;">Windows (HH:MM-HH:MM, comma-separated; blank = all other times)</label>
                <input type="text" id="wizard-fit-windows" style="width: 100%; padding: 6px; font-size: 12px; background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 4px; color: var(--text-primary);" value="16:00-22:59" placeholder="e.g. 16:00-22:59">
              </div>
            </fieldset>

            <!-- Buttons -->
            <div style="display: flex; gap: 8px; justify-content: flex-end; margin-top: 24px;">
              <button type="button" class="btn btn-secondary" onclick="window.touEditor.closeWizardModal()">Cancel</button>
              <button type="button" id="tou-wizard-finish-btn" class="btn btn-primary" onclick="window.touEditor.wizardFinish()">Finish & Load</button>
            </div>
            <div id="tou-wizard-feedback" style="margin-top: 12px; font-size: 12px; color: var(--text-muted); text-align: center;"></div>
          </form>
        </div>
      </div>
    `;

    host.innerHTML = html;

    // Expose editor API globally
    window.touEditor = {
      renderEditor,
      debounceResolve,
      openSaveDialog,
      closeSaveDialog,
      doSave,
      addImportBand,
      addFreeWindow,
      addFeedInBand,
      openBandEditor,
      closeBandPopover,
      renderRibbon,
      updateFormFromPlan,
      updatePlanFromForm,
      openWizardModal,
      closeWizardModal,
      wizardLoadTemplate,
      wizardFinish,
      exportPlan,
    };

    // Load form from plan
    updateFormFromPlan();

    // Apply read-only if needed
    if (!canEdit) {
      applyReadOnly();
    }

    // Wire form change handlers
    wireFormHandlers();
  }

  /**
   * Apply read-only styling and disable all inputs
   */
  function applyReadOnly() {
    const wrapper = document.querySelector('.tou-editor-wrapper');
    if (wrapper) {
      wrapper.style.opacity = '0.9';
    }

    // Disable all inputs in the editor
    const inputs = document.querySelectorAll('.tou-editor-wrapper input, .tou-editor-wrapper select, .tou-editor-wrapper button');
    inputs.forEach((el) => {
      if (el.id && el.id.startsWith('tou-')) {
        // Keep export and close buttons functional, disable everything else
        if (!el.id.includes('export') && !el.id.includes('close')) {
          el.disabled = true;
          el.style.cursor = 'not-allowed';
          if (el.tagName === 'INPUT' || el.tagName === 'SELECT') {
            el.style.opacity = '0.6';
          }
        }
      }
    });

    // Hide band edit popovers and buttons
    const bandPopover = document.getElementById('tou-band-popover');
    if (bandPopover) {
      bandPopover.style.display = 'none !important';
    }

    const editableListBtns = document.querySelectorAll('.tou-editor-wrapper button[onclick*="Add"]');
    editableListBtns.forEach(btn => btn.style.display = 'none');
  }

  /**
   * Wire up form input change listeners to trigger debounced resolve
   */
  function wireFormHandlers() {
    const inputs = document.querySelectorAll(
      '#tou-supply-charge, #tou-timezone, #tou-grid-charge-policy, #tou-billing-length, #tou-billing-anchor, #tou-vpp-enabled, #tou-version-from, #tou-version-until'
    );
    inputs.forEach((input) => {
      input.addEventListener('change', updatePlanFromFormAndResolve);
    });
  }

  /**
   * Update plan object from form values
   */
  function updatePlanFromForm() {
    if (!plan || !plan.plan) return;

    plan.timezone = document.getElementById('tou-timezone').value || 'Australia/Brisbane';
    plan.grid_charge_policy = document.getElementById('tou-grid-charge-policy').value || 'free_window_and_solar_only';
    plan.plan.supply_charge_c_per_day = parseFloat(document.getElementById('tou-supply-charge').value) || 0;

    if (!plan.plan.billing_cycle) plan.plan.billing_cycle = {};
    plan.plan.billing_cycle.length_days = parseInt(document.getElementById('tou-billing-length').value) || 28;
    plan.plan.billing_cycle.anchor_date = document.getElementById('tou-billing-anchor').value || '2026-06-01';

    if (!plan.plan.vpp) plan.plan.vpp = {};
    plan.plan.vpp.enabled = document.getElementById('tou-vpp-enabled').checked;

    if (!plan.plan.versions) plan.plan.versions = [{}];
    if (!plan.plan.versions[0]) plan.plan.versions[0] = {};
    plan.plan.versions[0].valid_from = document.getElementById('tou-version-from').value || '2026-06-01';
    plan.plan.versions[0].valid_until = document.getElementById('tou-version-until').value || null;
  }

  /**
   * Update form values from plan object
   */
  function updateFormFromPlan() {
    if (!plan) return;

    document.getElementById('tou-timezone').value = plan.timezone || 'Australia/Brisbane';
    document.getElementById('tou-grid-charge-policy').value = plan.grid_charge_policy || 'free_window_and_solar_only';
    document.getElementById('tou-supply-charge').value = plan.plan?.supply_charge_c_per_day || 148.5;
    document.getElementById('tou-billing-length').value = plan.plan?.billing_cycle?.length_days || 28;
    document.getElementById('tou-billing-anchor').value = plan.plan?.billing_cycle?.anchor_date || '2026-06-01';
    document.getElementById('tou-vpp-enabled').checked = plan.plan?.vpp?.enabled || false;
    document.getElementById('tou-version-from').value = plan.plan?.versions?.[0]?.valid_from || '2026-06-01';
    document.getElementById('tou-version-until').value = plan.plan?.versions?.[0]?.valid_until || '';

    renderImportBands();
    renderFreeWindows();
    renderFeedInBands();
  }

  /**
   * Update plan from form and trigger debounced resolve
   */
  function updatePlanFromFormAndResolve() {
    updatePlanFromForm();
    debounceResolve();
  }

  /**
   * Debounced call to resolveAndRenderRibbon
   */
  function debounceResolve() {
    if (resolveTimer) clearTimeout(resolveTimer);
    resolveTimer = setTimeout(resolveAndRenderRibbon, RESOLVE_DEBOUNCE_MS);
  }

  /**
   * POST /settings/tariff/resolve with current plan; render ribbon + cost
   */
  async function resolveAndRenderRibbon() {
    updatePlanFromForm();

    const statusEl = document.getElementById('tou-ribbon-status');
    const gapBadge = document.getElementById('tou-gap-badge');
    const saveBtn = document.getElementById('tou-save-btn');

    statusEl.textContent = 'Resolving...';
    gapBadge.style.display = 'none';

    try {
      const res = await fetch('/settings/tariff/resolve', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(plan),
      });

      const data = await res.json();

      if (data.ok) {
        slots = data.slots;
        const uncovered = data.coverage?.uncovered || 0;

        statusEl.textContent = uncovered > 0
          ? `Warning: ${uncovered} uncovered half-hour slots`
          : 'All hours covered';

        if (uncovered > 0) {
          gapBadge.style.display = 'block';
          gapBadge.textContent = `Warning: ${uncovered} uncovered half-hour slots — resolve before saving`;
          if (saveBtn) saveBtn.disabled = true;
        } else {
          if (saveBtn) saveBtn.disabled = false;
        }

        renderRibbon(data.slots);
        computeAndDisplayCost(data.slots);
      } else {
        statusEl.textContent = data.error || 'Invalid tariff config';
        if (gapBadge) gapBadge.style.display = 'block';
        if (gapBadge) gapBadge.textContent = `Error: ${data.error || 'Invalid plan'}`;
        if (saveBtn) saveBtn.disabled = true;
      }
    } catch (err) {
      statusEl.textContent = 'Resolve failed: ' + err.message;
      if (gapBadge) gapBadge.style.display = 'block';
      if (gapBadge) gapBadge.textContent = 'Error: ' + err.message;
      if (saveBtn) saveBtn.disabled = true;
    }
  }

  /**
   * Render the 24h ribbon with hour segments colored by descriptor
   */
  function renderRibbon(slots) {
    if (!slots || slots.length === 0) return;

    const mainRibbon = document.getElementById('tou-ribbon-main');
    const feedinRibbon = document.getElementById('tou-ribbon-feedin');
    const labels = document.getElementById('tou-ribbon-labels');

    mainRibbon.innerHTML = '';
    feedinRibbon.innerHTML = '';
    labels.innerHTML = '';

    const descriptorColorMap = {
      'free': 'var(--accent-green)',
      'four4free': 'var(--accent-green)',
      'peak': 'var(--accent-red)',
      'shoulder': '#4a5a7a',
      'off-peak': 'var(--accent-amber)',
      'offpeak': 'var(--accent-amber)',
      'off-peak-balance': 'var(--accent-amber)',
    };

    // Group slots by hour (2 per hour = 30 min each)
    const hourGroups = {};
    slots.forEach((slot, i) => {
      const hour = Math.floor(i / 2);
      if (!hourGroups[hour]) hourGroups[hour] = [];
      hourGroups[hour].push(slot);
    });

    // Render 24 hours
    for (let hour = 0; hour < 24; hour++) {
      const groupSlots = hourGroups[hour] || [];
      const descriptor = groupSlots[0]?.descriptor || 'unknown';
      const color = descriptorColorMap[descriptor] || '#6e7681';

      const hourSegment = document.createElement('div');
      hourSegment.className = 'hour-segment';
      hourSegment.style.flex = '1';
      hourSegment.style.background = color;
      hourSegment.style.position = 'relative';
      hourSegment.style.cursor = window.touEditorCanEdit ? 'pointer' : 'default';
      hourSegment.style.transition = 'all 0.15s ease';
      hourSegment.title = `${String(hour).padStart(2, '0')}:00 — ${descriptor}`;
      if (window.touEditorCanEdit) {
        hourSegment.addEventListener('click', () => openBandEditor(hour, descriptor, groupSlots[0]?.import_c));
      }
      mainRibbon.appendChild(hourSegment);

      // Feed-in sub-ribbon
      const feedinSegment = document.createElement('div');
      feedinSegment.style.flex = '1';
      feedinSegment.style.background = (groupSlots[0]?.export_c || 0) > 0 ? 'rgba(63, 185, 80, 0.3)' : 'transparent';
      feedinRibbon.appendChild(feedinSegment);
    }

    // Hour labels (0, 3, 6, 9, 12, 15, 18, 21, 24)
    for (let h = 0; h <= 24; h += 3) {
      const label = document.createElement('span');
      label.textContent = String(h).padStart(2, '0') + ':00';
      label.style.flex = (h < 24 ? 3 : 0);
      labels.appendChild(label);
    }
  }

  /**
   * Compute and display indicative daily cost
   */
  function computeAndDisplayCost(slots) {
    const costEl = document.getElementById('tou-cost-readout');
    if (!costEl) return;

    // Simple: assume 1 kWh per 30-min slot (48 kWh/day) and flat load
    const loadPerSlot = 1.0;
    let importCost = 0;
    let exportCredit = 0;

    slots.forEach((slot) => {
      importCost += (slot.import_c / 100) * loadPerSlot;
      exportCredit += (slot.export_c / 100) * loadPerSlot * 0.1; // assume small export fraction
    });

    const netCost = importCost - exportCredit;
    costEl.innerHTML = `
      <strong>Indicative daily cost (assuming 48 kWh flat load):</strong>
      $${(importCost).toFixed(2)} import ·
      $${(exportCredit).toFixed(2)} export ·
      <strong>$${(netCost).toFixed(2)} net/day</strong>
    `;
  }

  /**
   * Open band editor popover on ribbon click
   */
  function openBandEditor(hourIndex, descriptor, importRate) {
    const popover = document.getElementById('tou-band-popover');
    const title = document.getElementById('tou-popover-title');
    const content = document.getElementById('tou-popover-content');

    title.textContent = `Hour ${String(hourIndex).padStart(2, '0')}:00 - ${descriptor}`;

    content.innerHTML = `
      <div class="form-group" style="margin-bottom: 8px;">
        <label style="font-size: 11px; color: var(--text-muted); text-transform: uppercase;">Descriptor</label>
        <input type="text" value="${descriptor}" style="width: 100%; padding: 6px; font-size: 12px; background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 4px; color: var(--text-primary);" disabled>
      </div>
      <div class="form-group">
        <label style="font-size: 11px; color: var(--text-muted); text-transform: uppercase;">Import Rate (¢/kWh)</label>
        <input type="number" value="${importRate || 0}" step="0.01" style="width: 100%; padding: 6px; font-size: 12px; background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 4px; color: var(--text-primary);">
      </div>
    `;

    popover.style.display = 'block';
    popover.style.left = event.target.getBoundingClientRect().left + 'px';
    popover.style.top = (event.target.getBoundingClientRect().bottom + 8) + 'px';
  }

  /**
   * Close band popover
   */
  function closeBandPopover() {
    const popover = document.getElementById('tou-band-popover');
    popover.style.display = 'none';
  }

  /**
   * Render import bands rows
   */
  function renderImportBands() {
    const list = document.getElementById('tou-import-bands-list');
    if (!list || !plan.plan.versions?.[0]?.import_bands) return;

    list.innerHTML = plan.plan.versions[0].import_bands.map((band, i) => `
      <div style="padding: 12px; background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 4px; margin-bottom: 8px;">
        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 8px;">
          <div>
            <label style="font-size: 11px; color: var(--text-muted); display: block; margin-bottom: 4px;">Descriptor</label>
            <input type="text" class="form-input" style="font-size: 12px;" value="${band.descriptor || ''}" onchange="window.touEditor.updatePlanFromFormAndResolve()">
          </div>
          <div>
            <label style="font-size: 11px; color: var(--text-muted); display: block; margin-bottom: 4px;">Rate (¢/kWh)</label>
            <input type="number" class="form-input" style="font-size: 12px;" step="0.01" value="${band.rate_c_per_kwh || 0}" onchange="window.touEditor.updatePlanFromFormAndResolve()">
          </div>
        </div>
        <div>
          <label style="font-size: 11px; color: var(--text-muted); display: block; margin-bottom: 4px;">Windows (HH:MM-HH:MM, comma-separated)</label>
          <input type="text" class="form-input" style="font-size: 12px;" value="${(band.windows || []).join(', ')}" onchange="window.touEditor.updatePlanFromFormAndResolve()">
        </div>
        <button type="button" class="btn btn-secondary btn-sm" style="margin-top: 8px; width: 100%;" onclick="window.touEditor.removeImportBand(${i})">Remove</button>
      </div>
    `).join('');
  }

  window.touEditor.removeImportBand = function(i) {
    if (plan.plan.versions?.[0]?.import_bands) {
      plan.plan.versions[0].import_bands.splice(i, 1);
      renderImportBands();
      debounceResolve();
    }
  };

  /**
   * Render free windows rows
   */
  function renderFreeWindows() {
    const list = document.getElementById('tou-free-windows-list');
    if (!list || !plan.plan.versions?.[0]?.free_windows) return;

    list.innerHTML = plan.plan.versions[0].free_windows.map((fw, i) => `
      <div style="padding: 12px; background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 4px; margin-bottom: 8px;">
        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 8px;">
          <div>
            <label style="font-size: 11px; color: var(--text-muted); display: block; margin-bottom: 4px;">Name</label>
            <input type="text" class="form-input" style="font-size: 12px;" value="${fw.name || ''}" onchange="window.touEditor.updatePlanFromFormAndResolve()">
          </div>
          <div>
            <label style="font-size: 11px; color: var(--text-muted); display: block; margin-bottom: 4px;">Daily Cap (kWh)</label>
            <input type="number" class="form-input" style="font-size: 12px;" step="0.1" value="${fw.cap_kwh_per_day || 50}" onchange="window.touEditor.updatePlanFromFormAndResolve()">
          </div>
        </div>
        <div style="margin-bottom: 8px;">
          <label style="font-size: 11px; color: var(--text-muted); display: block; margin-bottom: 4px;">Windows (HH:MM-HH:MM, comma-separated)</label>
          <input type="text" class="form-input" style="font-size: 12px;" value="${(fw.windows || []).join(', ')}" onchange="window.touEditor.updatePlanFromFormAndResolve()">
        </div>
        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px;">
          <div>
            <label style="font-size: 11px; color: var(--text-muted); display: block; margin-bottom: 4px;">Channel</label>
            <select class="form-input" style="font-size: 12px;" onchange="window.touEditor.updatePlanFromFormAndResolve()">
              <option ${fw.applies_to_channel === 'general' ? 'selected' : ''}>general</option>
              <option ${fw.applies_to_channel === 'controlled_load' ? 'selected' : ''}>controlled_load</option>
            </select>
          </div>
          <div>
            <label style="font-size: 11px; color: var(--text-muted); display: block; margin-bottom: 4px;">Fall Back To</label>
            <input type="text" class="form-input" style="font-size: 12px;" value="${fw.over_cap_falls_back_to || 'shoulder'}" onchange="window.touEditor.updatePlanFromFormAndResolve()">
          </div>
        </div>
        <button type="button" class="btn btn-secondary btn-sm" style="margin-top: 8px; width: 100%;" onclick="window.touEditor.removeFreeWindow(${i})">Remove</button>
      </div>
    `).join('');
  }

  window.touEditor.removeFreeWindow = function(i) {
    if (plan.plan.versions?.[0]?.free_windows) {
      plan.plan.versions[0].free_windows.splice(i, 1);
      renderFreeWindows();
      debounceResolve();
    }
  };

  /**
   * Render feed-in bands rows
   */
  function renderFeedInBands() {
    const list = document.getElementById('tou-feedin-bands-list');
    if (!list || !plan.plan.versions?.[0]?.feed_in_bands) return;

    list.innerHTML = plan.plan.versions[0].feed_in_bands.map((band, i) => `
      <div style="padding: 12px; background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 4px; margin-bottom: 8px;">
        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 8px;">
          <div>
            <label style="font-size: 11px; color: var(--text-muted); display: block; margin-bottom: 4px;">Name</label>
            <input type="text" class="form-input" style="font-size: 12px;" value="${band.name || ''}" onchange="window.touEditor.updatePlanFromFormAndResolve()">
          </div>
          <div>
            <label style="font-size: 11px; color: var(--text-muted); display: block; margin-bottom: 4px;">Rate (¢/kWh)</label>
            <input type="number" class="form-input" style="font-size: 12px;" step="0.01" value="${band.rate_c_per_kwh || 0}" onchange="window.touEditor.updatePlanFromFormAndResolve()">
          </div>
        </div>
        <div>
          <label style="font-size: 11px; color: var(--text-muted); display: block; margin-bottom: 4px;">Windows (HH:MM-HH:MM, comma-separated; leave blank for all other times)</label>
          <input type="text" class="form-input" style="font-size: 12px;" value="${(band.windows || []).join(', ')}" onchange="window.touEditor.updatePlanFromFormAndResolve()">
        </div>
        <button type="button" class="btn btn-secondary btn-sm" style="margin-top: 8px; width: 100%;" onclick="window.touEditor.removeFeedInBand(${i})">Remove</button>
      </div>
    `).join('');
  }

  window.touEditor.removeFeedInBand = function(i) {
    if (plan.plan.versions?.[0]?.feed_in_bands) {
      plan.plan.versions[0].feed_in_bands.splice(i, 1);
      renderFeedInBands();
      debounceResolve();
    }
  };

  /**
   * Add a new import band
   */
  function addImportBand() {
    if (!plan.plan.versions?.[0]) return;
    plan.plan.versions[0].import_bands = plan.plan.versions[0].import_bands || [];
    plan.plan.versions[0].import_bands.push({
      descriptor: 'new-band',
      windows: [],
      rate_c_per_kwh: 30.0,
    });
    renderImportBands();
    debounceResolve();
  }

  /**
   * Add a new free window
   */
  function addFreeWindow() {
    if (!plan.plan.versions?.[0]) return;
    plan.plan.versions[0].free_windows = plan.plan.versions[0].free_windows || [];
    plan.plan.versions[0].free_windows.push({
      name: 'new-free-window',
      windows: [],
      rate_c_per_kwh: 0.0,
      cap_kwh_per_day: 50.0,
      applies_to_channel: 'general',
      over_cap_falls_back_to: 'shoulder',
    });
    renderFreeWindows();
    debounceResolve();
  }

  /**
   * Add a new feed-in band
   */
  function addFeedInBand() {
    if (!plan.plan.versions?.[0]) return;
    plan.plan.versions[0].feed_in_bands = plan.plan.versions[0].feed_in_bands || [];
    plan.plan.versions[0].feed_in_bands.push({
      name: 'new-feedin-band',
      windows: [],
      rate_c_per_kwh: 10.0,
    });
    renderFeedInBands();
    debounceResolve();
  }

  /**
   * Open save confirmation dialog
   */
  function openSaveDialog() {
    updatePlanFromForm();

    const modal = document.getElementById('tou-save-modal');
    const diffList = document.getElementById('tou-diff-list');

    // Compute diffs
    const diffs = computeDiffs(savedPlan, plan);

    if (diffs.length === 0) {
      diffList.innerHTML = '<p style="color: var(--text-muted); font-size: 12px;">No changes detected.</p>';
    } else {
      diffList.innerHTML = '<div style="font-size: 13px;"><strong>Changes:</strong><ul style="margin: 8px 0; padding-left: 20px;">' +
        diffs.map((d) => `<li>${d}</li>`).join('') +
        '</ul></div>';
    }

    modal.style.display = 'flex';
  }

  /**
   * Simple diff computation (field-level)
   */
  function computeDiffs(old, new_) {
    const diffs = [];
    if (old.timezone !== new_.timezone) diffs.push(`Timezone: ${old.timezone} → ${new_.timezone}`);
    if (old.grid_charge_policy !== new_.grid_charge_policy) diffs.push(`Grid charge policy: ${old.grid_charge_policy} → ${new_.grid_charge_policy}`);
    if (old.plan?.supply_charge_c_per_day !== new_.plan?.supply_charge_c_per_day) {
      diffs.push(`Supply charge: ${old.plan?.supply_charge_c_per_day} → ${new_.plan?.supply_charge_c_per_day}¢/day`);
    }
    if (JSON.stringify(old.plan?.billing_cycle) !== JSON.stringify(new_.plan?.billing_cycle)) {
      diffs.push(`Billing cycle updated`);
    }
    if (JSON.stringify(old.plan?.vpp) !== JSON.stringify(new_.plan?.vpp)) {
      diffs.push(`VPP settings updated`);
    }
    if (JSON.stringify(old.plan?.versions) !== JSON.stringify(new_.plan?.versions)) {
      diffs.push(`Import/export/free windows or credits updated`);
    }
    return diffs;
  }

  /**
   * Close save dialog
   */
  function closeSaveDialog() {
    const modal = document.getElementById('tou-save-modal');
    modal.style.display = 'none';
  }

  /**
   * Perform the actual save via POST /settings/tariff
   */
  async function doSave() {
    const confirmBtn = document.getElementById('tou-confirm-save-btn');
    const feedback = document.getElementById('tou-save-feedback');
    const saveBtn = document.getElementById('tou-save-btn');

    confirmBtn.disabled = true;
    confirmBtn.textContent = 'Saving...';
    feedback.textContent = 'Sending to server...';

    try {
      const res = await fetch('/settings/tariff', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(plan),
      });

      const data = await res.json();

      if (data.ok) {
        feedback.textContent = 'Saved successfully!';
        feedback.style.color = 'var(--accent-green)';
        savedPlan = JSON.parse(JSON.stringify(plan));
        setTimeout(() => {
          closeSaveDialog();
          if (saveBtn) saveBtn.textContent = 'Save Plan';
          if (saveBtn) saveBtn.disabled = false;
        }, 1000);
      } else {
        feedback.textContent = 'Error: ' + (data.errors?.[0] || data.error || 'Unknown error');
        feedback.style.color = 'var(--accent-red)';
        confirmBtn.disabled = false;
        confirmBtn.textContent = 'Confirm & Save';
      }
    } catch (err) {
      feedback.textContent = 'Network error: ' + err.message;
      feedback.style.color = 'var(--accent-red)';
      confirmBtn.disabled = false;
      confirmBtn.textContent = 'Confirm & Save';
    }
  }

  /**
   * Open EFS wizard modal
   */
  function openWizardModal() {
    const modal = document.getElementById('tou-wizard-modal');
    const feedback = document.getElementById('tou-wizard-feedback');
    if (modal) {
      modal.style.display = 'flex';
      feedback.textContent = '';
      loadWizardTemplates();
    }
  }

  /**
   * Close wizard modal
   */
  function closeWizardModal() {
    const modal = document.getElementById('tou-wizard-modal');
    if (modal) modal.style.display = 'none';
  }

  /**
   * Load preset templates from backend
   */
  async function loadWizardTemplates() {
    const select = document.getElementById('tou-wizard-template-select');
    if (!select) return;

    try {
      const res = await fetch('/settings/tariff/templates');
      const data = await res.json();

      if (data.ok && data.templates) {
        // Keep the first "manual" option
        const currentValue = select.value;
        select.innerHTML = '<option value="">-- Enter manually --</option>';
        data.templates.forEach(tpl => {
          const opt = document.createElement('option');
          opt.value = tpl.id;
          opt.textContent = tpl.name;
          opt.dataset.tariff = JSON.stringify(tpl.tariff);
          select.appendChild(opt);
        });
        select.value = currentValue;
      }
    } catch (err) {
      console.warn('Failed to load templates:', err);
    }
  }

  /**
   * Load template into wizard form
   */
  function wizardLoadTemplate() {
    const select = document.getElementById('tou-wizard-template-select');
    if (!select || !select.value) return;

    const opt = select.options[select.selectedIndex];
    if (!opt.dataset.tariff) return;

    try {
      const tariff = JSON.parse(opt.dataset.tariff);
      const version = tariff.plan?.versions?.[0];
      if (!version) return;

      // Populate form from template
      document.getElementById('wizard-supply-charge').value = tariff.plan?.supply_charge_c_per_day || 148.5;
      document.getElementById('wizard-timezone').value = tariff.timezone || 'Australia/Brisbane';
      document.getElementById('wizard-billing-length').value = tariff.plan?.billing_cycle?.length_days || 28;
      document.getElementById('wizard-anchor-date').value = tariff.plan?.billing_cycle?.anchor_date || '2026-06-01';

      // Peak band
      const peakBand = version.import_bands?.find(b => b.descriptor === 'peak');
      if (peakBand) {
        document.getElementById('wizard-peak-rate').value = peakBand.rate_c_per_kwh || 55.55;
        document.getElementById('wizard-peak-windows').value = (peakBand.windows || []).join(', ');
      }

      // Free window
      const freeWindow = version.free_windows?.[0];
      if (freeWindow) {
        document.getElementById('wizard-free-name').value = freeWindow.name || 'free';
        document.getElementById('wizard-free-cap').value = freeWindow.cap_kwh_per_day || 50;
        document.getElementById('wizard-free-windows').value = (freeWindow.windows || []).join(', ');
      }

      // Off-peak band
      const offPeakBand = version.import_bands?.find(b => b.descriptor === 'off-peak-balance');
      if (offPeakBand) {
        document.getElementById('wizard-offpeak-rate').value = offPeakBand.rate_c_per_kwh || 28.6;
      }

      // Shoulder band
      const shoulderBand = version.import_bands?.find(b => b.descriptor === 'shoulder');
      if (shoulderBand) {
        document.getElementById('wizard-shoulder-rate').value = shoulderBand.rate_c_per_kwh || 34.1;
      }

      // FiT band
      const fitBand = version.feed_in_bands?.find(b => b.descriptor !== 'default-fit' && b.rate_c_per_kwh > 0);
      if (fitBand) {
        document.getElementById('wizard-fit-rate').value = fitBand.rate_c_per_kwh || 8;
        document.getElementById('wizard-fit-windows').value = (fitBand.windows || []).join(', ');
      }
    } catch (err) {
      console.error('Failed to load template:', err);
    }
  }

  /**
   * Assemble wizard input into a complete plan and validate/load it
   */
  async function wizardFinish() {
    const feedback = document.getElementById('tou-wizard-feedback');
    const finishBtn = document.getElementById('tou-wizard-finish-btn');
    const form = document.getElementById('tou-wizard-form');

    // Collect wizard values
    const supplyCharge = parseFloat(document.getElementById('wizard-supply-charge').value) || 148.5;
    const timezone = document.getElementById('wizard-timezone').value || 'Australia/Brisbane';
    const billingLength = parseInt(document.getElementById('wizard-billing-length').value) || 28;
    const anchorDate = document.getElementById('wizard-anchor-date').value || '2026-06-01';

    const peakRate = parseFloat(document.getElementById('wizard-peak-rate').value) || 55.55;
    const peakWindows = document.getElementById('wizard-peak-windows').value.split(',').map(s => s.trim()).filter(s => s);

    const freeName = document.getElementById('wizard-free-name').value || 'free';
    const freeCap = parseFloat(document.getElementById('wizard-free-cap').value) || 50;
    const freeWindows = document.getElementById('wizard-free-windows').value.split(',').map(s => s.trim()).filter(s => s);
    const offPeakRate = parseFloat(document.getElementById('wizard-offpeak-rate').value) || 28.6;

    const shoulderRate = parseFloat(document.getElementById('wizard-shoulder-rate').value) || 34.1;

    const fitRate = parseFloat(document.getElementById('wizard-fit-rate').value) || 8;
    const fitWindows = document.getElementById('wizard-fit-windows').value.split(',').map(s => s.trim()).filter(s => s);

    // Build plan object
    const newPlan = {
      type: 'tou',
      timezone,
      grid_charge_policy: 'free_window_and_solar_only',
      plan: {
        supply_charge_c_per_day: supplyCharge,
        billing_cycle: {
          length_days: billingLength,
          anchor_date: anchorDate,
        },
        vpp: { enabled: false },
        versions: [
          {
            valid_from: anchorDate,
            valid_until: null,
            import_bands: [
              { descriptor: 'peak', windows: peakWindows, rate_c_per_kwh: peakRate },
              { descriptor: 'off-peak-balance', windows: freeWindows, rate_c_per_kwh: offPeakRate },
              { descriptor: 'shoulder', windows: [], rate_c_per_kwh: shoulderRate },
            ],
            free_windows: [
              {
                name: freeName,
                windows: freeWindows,
                rate_c_per_kwh: 0.0,
                cap_kwh_per_day: freeCap,
                applies_to_channel: 'general',
                over_cap_falls_back_to: 'off-peak-balance',
              },
            ],
            feed_in_bands: [
              { name: 'fit', windows: fitWindows, rate_c_per_kwh: fitRate },
              { name: 'default-fit', windows: [], rate_c_per_kwh: 0 },
            ],
            credits: [],
          },
        ],
      },
    };

    // Validate via POST /settings/tariff/resolve
    finishBtn.disabled = true;
    feedback.textContent = 'Validating...';
    feedback.style.color = 'var(--text-muted)';

    try {
      const res = await fetch('/settings/tariff/resolve', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(newPlan),
      });

      const data = await res.json();

      if (!data.ok) {
        feedback.textContent = 'Error: ' + (data.errors?.[0] || data.error || 'Invalid tariff config');
        feedback.style.color = 'var(--accent-red)';
        finishBtn.disabled = false;
        return;
      }

      // Load plan into editor
      plan = newPlan;
      savedPlan = JSON.parse(JSON.stringify(plan));
      updateFormFromPlan();
      debounceResolve();

      feedback.textContent = 'Plan loaded! Review and save.';
      feedback.style.color = 'var(--accent-green)';

      setTimeout(() => {
        closeWizardModal();
      }, 500);
    } catch (err) {
      feedback.textContent = 'Network error: ' + err.message;
      feedback.style.color = 'var(--accent-red)';
      finishBtn.disabled = false;
    }
  }

  /**
   * Export current plan as JSON
   */
  function exportPlan() {
    updatePlanFromForm();
    const json = JSON.stringify(plan, null, 2);
    const blob = new Blob([json], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'tariff-plan.json';
    a.click();
    URL.revokeObjectURL(url);
  }

})();
