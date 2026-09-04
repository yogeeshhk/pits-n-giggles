class SetupGuide {
  constructor() {
    this.button = document.getElementById('setup-guide-btn');
    this.gameSelect = document.getElementById('setup-guide-game-select');
    this.select = document.getElementById('setup-guide-circuit-select');
    this.content = document.getElementById('setup-guide-content');
    this.emptyState = document.getElementById('setup-guide-empty');
    this.tempTable = document.getElementById('setup-guide-temp-table');
    this.fixList = document.getElementById('setup-guide-fixes');
    this.setups = [];
    this.guides = new Map();
    this.dataByGameCircuit = new Map();
    this.currentCircuit = null;
    this.currentGameYear = '2025';
    this.liveCircuit = null;
    this.liveGameYear = '2025';
    this.manualGameSelection = false;
    this.manualCircuitSelection = false;
    this.modal = document.getElementById('setupGuideModal');

    if (!this.button || !this.gameSelect || !this.select || !this.content) {
      return;
    }

    this.gameSelect.addEventListener('change', () => {
      this.manualGameSelection = true;
      this.manualCircuitSelection = false;
      this.currentGameYear = this.gameSelect.value;
      this.activateGuide(this.currentGameYear, this.currentCircuit);
    });
    this.select.addEventListener('change', () => {
      this.manualCircuitSelection = true;
      this.currentCircuit = this.select.value;
      this.renderCircuit(this.select.value);
    });
    this.modal?.addEventListener('show.bs.modal', () => {
      this.manualGameSelection = false;
      this.manualCircuitSelection = false;
      this.currentGameYear = this.liveGameYear;
      this.currentCircuit = this.liveCircuit;
      this.gameSelect.value = this.currentGameYear;
      this.activateGuide(this.currentGameYear, this.currentCircuit);
    });
    this.load();
  }

  async load() {
    try {
      const guides = await Promise.all([
        this.fetchGuide('2025', '/static/data/f1_25_setups.json'),
        this.fetchGuide('2026', '/static/data/f1_26_setups.json')
      ]);
      guides.forEach(({ gameYear, data }) => {
        const setups = data.setups || [];
        this.guides.set(gameYear, data);
        this.dataByGameCircuit.set(gameYear, new Map(
          setups.map(setup => [this.normalizeCircuit(setup.circuit), setup])
        ));
      });
      this.activateGuide(this.currentGameYear, this.currentCircuit);
    } catch (error) {
      console.error('Failed to load setup guide:', error);
      this.showEmptyState('Setup guide could not be loaded.');
    }
  }

  async fetchGuide(gameYear, url) {
    const response = await fetch(url);
    if (!response.ok) {
      throw new Error(`setup guide ${gameYear} returned ${response.status}`);
    }
    return { gameYear, data: await response.json() };
  }

  updateFromTelemetry(data) {
    this.liveGameYear = this.resolveGameYear(data?.['f1-game-year']);
    this.liveCircuit = data?.circuit;

    if (this.isModalOpen() && (this.manualGameSelection || this.manualCircuitSelection)) {
      return;
    }

    this.currentGameYear = this.liveGameYear;
    this.currentCircuit = this.liveCircuit;
    if (this.gameSelect) {
      this.gameSelect.value = this.currentGameYear;
    }
    this.activateGuide(this.currentGameYear, this.currentCircuit);
  }

  updateCircuit(circuit) {
    this.currentCircuit = circuit;
    this.activateGuide(this.currentGameYear, circuit);
  }

  isModalOpen() {
    return this.modal?.classList.contains('show') || false;
  }

  resolveGameYear(gameYear) {
    return Number(gameYear) >= 2026 ? '2026' : '2025';
  }

  activateGuide(gameYear, circuit) {
    const guide = this.guides.get(gameYear);
    if (!guide) {
      return;
    }
    this.setups = guide.setups || [];
    this.populateCircuitSelect();
    this.renderTyreTemps(guide.tyreTemps || []);
    this.renderFixes(guide.fixes || [], guide.faq || []);

    const matchedSetup = this.findSetup(circuit);
    if (matchedSetup) {
      this.select.value = matchedSetup.circuit;
      this.renderCircuit(matchedSetup.circuit);
    } else if (!this.select.value && this.setups[0]) {
      this.select.value = this.setups[0].circuit;
      this.renderCircuit(this.setups[0].circuit);
    }
  }

  populateCircuitSelect() {
    this.select.textContent = '';
    this.setups.forEach(setup => {
      const option = document.createElement('option');
      option.value = setup.circuit;
      option.textContent = setup.circuit;
      this.select.appendChild(option);
    });
  }

  findSetup(circuit) {
    const dataByCircuit = this.dataByGameCircuit.get(this.currentGameYear);
    return dataByCircuit?.get(this.normalizeCircuit(circuit));
  }

  normalizeCircuit(circuit) {
    if (!circuit || circuit === '---') {
      return '';
    }

    const aliases = {
      'melbourne': 'australia',
      'shanghai': 'china',
      'sakhir': 'bahrain',
      'sakhir bahrain': 'bahrain',
      'catalunya': 'spain',
      'montreal': 'canada',
      'silverstone': 'britain',
      'silverstone reverse': 'britain',
      'great britain': 'britain',
      'hungaroring': 'hungary',
      'spa': 'belgium',
      'monza': 'italy',
      'suzuka': 'japan',
      'baku': 'azerbaijan',
      'baku azerbaijan': 'azerbaijan',
      'zandvoort': 'netherlands',
      'zandvoort reverse': 'netherlands',
      'texas': 'united states',
      'jeddah': 'saudi arabia',
      'losail': 'qatar'
    };

    const normalized = circuit
      .replace(/_Reverse$/i, ' Reverse')
      .replace(/_/g, ' ')
      .trim()
      .toLowerCase();
    return aliases[normalized] || normalized;
  }

  renderCircuit(circuit) {
    const dataByCircuit = this.dataByGameCircuit.get(this.currentGameYear);
    const setup = dataByCircuit?.get(this.normalizeCircuit(circuit));
    if (!setup) {
      this.showEmptyState(`No ${this.getGameLabel()} setup available for this circuit.`);
      return;
    }

    this.emptyState.style.display = 'none';
    this.content.style.display = '';
    this.content.textContent = '';

    const summary = document.createElement('div');
    summary.className = 'setup-guide-summary';
    this.appendSummaryItem(summary, 'Aero', setup.aero);
    this.appendSummaryItem(summary, 'Diff', setup.differential);
    this.appendSummaryItem(summary, 'Brakes', setup.brakes);
    this.appendSummaryItem(summary, 'Compounds', setup.compounds);
    this.appendSummaryItem(summary, '50% Laps', setup['laps-50']);
    this.content.appendChild(summary);

    const detailGrid = document.createElement('div');
    detailGrid.className = 'setup-guide-detail-grid';
    [
      ['Susp. Geometry', setup['suspension-geometry']],
      ['Suspension', setup.suspension],
      ['Tyres Q', setup['tyres-q']],
      ['Tyres R', setup['tyres-r']],
      ['50% Strategy', setup['strategy-50']],
      ['Notes', setup.notes]
    ].forEach(([label, value]) => this.appendDetailItem(detailGrid, label, value));
    this.content.appendChild(detailGrid);
  }

  appendSummaryItem(parent, label, value) {
    const item = document.createElement('div');
    item.className = 'setup-guide-summary-item';
    const labelEl = document.createElement('span');
    labelEl.textContent = label;
    const valueEl = document.createElement('strong');
    valueEl.textContent = this.formatValue(value);
    item.append(labelEl, valueEl);
    parent.appendChild(item);
  }

  appendDetailItem(parent, label, value) {
    const item = document.createElement('div');
    item.className = 'setup-guide-detail-item';
    const labelEl = document.createElement('span');
    labelEl.textContent = label;
    const valueEl = document.createElement('strong');
    valueEl.textContent = this.formatValue(value);
    item.append(labelEl, valueEl);
    parent.appendChild(item);
  }

  formatValue(value) {
    return value === null || value === undefined || value === '' ? 'Unavailable' : value;
  }

  renderTyreTemps(tyreTemps) {
    if (!this.tempTable) {
      return;
    }
    this.tempTable.textContent = '';
    tyreTemps.forEach(row => {
      const tr = document.createElement('tr');
      [row.compound, row['temp-range-c'], row['temp-range-f']].forEach(value => {
        const td = document.createElement('td');
        td.textContent = value;
        tr.appendChild(td);
      });
      this.tempTable.appendChild(tr);
    });
  }

  renderFixes(fixes, faq = []) {
    if (!this.fixList) {
      return;
    }
    this.fixList.textContent = '';
    const items = [
      ...fixes.map(item => ({ title: item.issue, text: item.fix })),
      ...faq.map(item => ({ title: item.question, text: item.answer }))
    ];
    items.forEach(item => {
      const row = document.createElement('div');
      row.className = 'setup-guide-fix-item';
      const issue = document.createElement('strong');
      issue.textContent = item.title;
      const fix = document.createElement('span');
      fix.textContent = item.text;
      row.append(issue, fix);
      this.fixList.appendChild(row);
    });
  }

  getGameLabel() {
    const guide = this.guides.get(this.currentGameYear);
    return guide?.game || `F1 ${this.currentGameYear.slice(-2)}`;
  }

  showEmptyState(message) {
    this.content.style.display = 'none';
    this.emptyState.style.display = '';
    this.emptyState.textContent = message;
  }
}
