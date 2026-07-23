/* Gate Walkthrough Calibration — inline panel on the Run page.
 * Driven by the server `gate_cal_state` snapshot; requests it on page load so
 * the state survives navigation. Start opens the timed window; pilots carry
 * powered-up quads through the gate; per-seat status updates live. */
(function () {
	'use strict';

	if (window.__rhGateCal) { return; }
	window.__rhGateCal = true;
	if (typeof io === 'undefined') { return; }

	var socket = null, state = {}, ticker = null, endsAt = 0;
	// Always collapsed on page load. Expanding is session-only (not persisted);
	// forced collapsed while a race is staged/running, auto-expanded while a
	// calibration window is open and when a new channel-change (stale)
	// recommendation appears.
	var userOpen = null, staleAuto = false, lastStaleKey = '';

	function isOpen() {
		if (state.race_active) { return false; }
		if (state.phase === 'running') { return true; }
		if (userOpen !== null) { return userOpen; }
		return staleAuto;
	}

	// ------------------------------------------------------------------ theme
	var theme = 'dark';
	var lightMq = window.matchMedia ? window.matchMedia('(prefers-color-scheme: light)') : null;
	function isLight() {
		if (theme === 'light') return true;
		if (theme === 'auto') return !!(lightMq && lightMq.matches);
		return false;
	}

	function ensureCss() {
		if (document.getElementById('rh-gc-css')) { return; }
		var l = document.createElement('link');
		l.id = 'rh-gc-css'; l.rel = 'stylesheet';
		l.href = '/gate_calibration/static/gate_calibration.css';
		(document.head || document.documentElement).appendChild(l);
	}
	function el(tag, cls, html) {
		var e = document.createElement(tag);
		if (cls) { e.className = cls; }
		if (html != null) { e.innerHTML = html; }
		return e;
	}
	function onRunPage() { return !!document.getElementById('leaderboard'); }

	var panel;
	// Shared dock for our plugin panels (gate calibration, auto marshalling, …)
	// above the pilot table. Whichever plugin loads first creates it; panels
	// order themselves via CSS `order` and wrap onto a new row when narrow.
	function dock() {
		var d = document.getElementById('rh-plugin-dock');
		if (d) { return d; }
		var anchor = document.getElementById('leaderboard');
		if (!anchor || !anchor.parentNode) { return null; }
		d = el('div', 'rh-plugin-dock');
		d.id = 'rh-plugin-dock';
		anchor.parentNode.insertBefore(d, anchor);
		return d;
	}
	function place() {
		if (!panel) { return; }
		var d = dock();
		if (d) {
			if (panel.parentNode !== d) { d.appendChild(panel); }
			return;
		}
		if (panel.parentNode) { return; }
		if (document.body) { document.body.insertBefore(panel, document.body.firstChild); }
	}
	function ensurePanel() {
		if (panel) { place(); return panel; }
		panel = el('div', 'rh-gc'); panel.id = 'rh-gc';
		panel.innerHTML =
			'<div class="rh-gc-head"><span class="rh-gc-chev">▸</span>' +
			'<div class="rh-gc-title"><span class="rh-gc-spark">⌖</span> Gate Walkthrough Calibration</div>' +
			'<div class="rh-gc-headsum"></div>' +
			'<div class="rh-gc-timer"></div></div>' +
			'<div class="rh-gc-body"><div class="rh-gc-ctl"></div>' +
			'<div class="rh-gc-track"><div class="rh-gc-bar"></div></div>' +
			'<div class="rh-gc-rows"></div>' +
			'<div class="rh-gc-foot"></div></div>';
		panel.querySelector('.rh-gc-head').addEventListener('click', function (e) {
			if (e.target.closest('button, input')) { return; }
			if (state.race_active || state.phase === 'running') { return; }
			userOpen = !isOpen(); staleAuto = false;
			render(state);
		});
		place();
		return panel;
	}
	function q(sel) { return panel.querySelector(sel); }

	// ------------------------------------------------------------- countdown
	function tick() {
		var t = q('.rh-gc-timer'), bar = q('.rh-gc-bar');
		if (!t) { return; }
		var left = Math.max(0, (endsAt - Date.now()) / 1000);
		t.textContent = left.toFixed(1) + 's';
		var total = state.secs || 30;
		if (bar) { bar.style.width = Math.max(0, Math.min(100, left / total * 100)) + '%'; }
	}
	function startTicker() { stopTicker(); ticker = setInterval(tick, 100); }
	function stopTicker() { if (ticker) { clearInterval(ticker); ticker = null; } }

	// ------------------------------------------------------------ seat cells
	var STATUS = {
		wait: ['waiting for pass', 'rh-gc-c-idle'],
		pass: ['pass detected…', 'rh-gc-c-info'],
		set: ['calibrated', 'rh-gc-c-ok'],
		updated: ['re-calibrated', 'rh-gc-c-ok'],
		nopass: ['no pass', 'rh-gc-c-review'],
		ok: ['calibrated', 'rh-gc-c-ok'],
		stale: ['channel changed — recalibrate', 'rh-gc-c-warn'],
		uncal: ['not calibrated', 'rh-gc-c-idle']
	};
	function chip(text, cls, title) {
		return '<span class="rh-gc-chip ' + cls + '"' +
			(title ? ' title="' + title + '"' : '') + '>' + text + '</span>';
	}
	function seatCell(s) {
		var c = el('div', 'rh-gc-cell rh-gc-' + (s.status || 'uncal'));
		var lab = STATUS[s.status] || [s.status, 'rh-gc-c-idle'];
		var top = el('div', 'rh-gc-ctop');
		top.innerHTML = '<span class="rh-gc-seat">S' + ((s.seat | 0) + 1) + '</span>' +
			'<span class="rh-gc-name">' + (s.callsign || 'Seat') + '</span>' +
			'<span class="rh-gc-chan">' + (s.chan || '') + '</span>';
		if (state.phase !== 'running' && !state.race_active) {
			var b = el('button', 'rh-gc-ico' +
				(s.status === 'stale' ? ' rh-gc-ico-warn' : ''), '↻');
			b.title = 'Calibrate ' + (s.callsign || 'this pilot') + ' only — same ' +
				'window duration as the main Start button';
			b.addEventListener('click', function (e) {
				e.stopPropagation();
				// use the duration from the panel input, same as the main Start
				var inp = panel && panel.querySelector('.rh-gc-secs');
				var v = inp ? parseInt(inp.value, 10) : 0;
				var payload = { seat: s.seat };
				if (v > 0) { payload.secs = v; }
				socket.emit('gate_cal_start', payload);
			});
			top.appendChild(b);
		}
		c.appendChild(top);
		var bot = el('div', 'rh-gc-cbot');
		var h = '';
		if (s.status === 'stale') {
			h += chip(lab[0], lab[1], 'Calibrated on ' + (s.cal_chan || '?') +
				', now on ' + (s.chan || '?') + ' — run a new calibration window.');
		} else if (s.enter != null) {
			h += '<span class="rh-gc-thr">' + s.enter + '/' + s.exit + '</span>' +
				chip(lab[0], lab[1], s.peak != null ? 'Pass peak ' + s.peak : null);
			if (s.age_min != null) {
				h += '<span class="rh-gc-age">' +
					(s.age_min < 60 ? s.age_min + 'm' : Math.floor(s.age_min / 60) + 'h') +
					' ago</span>';
			}
		} else if (s.status === 'pass') {
			h += '<span class="rh-gc-spin"></span>' + chip(lab[0], lab[1],
				'Peak ' + (s.peak != null ? s.peak : '?') + ' — carry the quad away to finish');
		} else {
			h += chip(lab[0], lab[1]);
		}
		bot.innerHTML = h;
		c.appendChild(bot);
		return c;
	}

	// ---------------------------------------------------------------- render
	function render(s) {
		state = s || {};
		if (state.theme && state.theme !== theme) { theme = state.theme; }
		if (!onRunPage()) { return; }
		ensurePanel();
		var phase = state.phase || 'idle';
		// a calibration window opening expands the panel for this page session
		// (stays open on 'done' so the results are visible; reload = collapsed)
		if (phase === 'running') { userOpen = true; }

		// channel-change recommendations: auto-expand when a new one appears
		var stale = (state.seats || []).filter(function (r) { return r.status === 'stale'; });
		var staleKey = stale.map(function (r) { return r.seat + ':' + r.chan; }).join(',');
		if (staleKey && staleKey !== lastStaleKey) { staleAuto = true; }
		if (!staleKey) { staleAuto = false; }
		lastStaleKey = staleKey;

		var open = isOpen();
		panel.className = 'rh-gc rh-gc-phase-' + phase +
			(open ? '' : ' rh-gc-collapsed') + (isLight() ? ' rh-gc-light' : '');
		panel.querySelector('.rh-gc-chev').textContent = open ? '▾' : '▸';

		// collapsed-header summary: the recalibration recommendation must be
		// visible without expanding
		var sum = panel.querySelector('.rh-gc-headsum'); sum.innerHTML = '';
		if (stale.length) {
			var names = stale.map(function (r) { return r.callsign; }).join(', ');
			sum.innerHTML = chip('⚠ recalibrate: ' + names, 'rh-gc-c-adaptive',
				'Channel changed since the last walk-through calibration. ' +
				'Open the panel and press ↻ on the pilot to recalibrate.');
		} else if (!open) {
			if (state.race_active) {
				sum.innerHTML = '<span class="rh-gc-headmut">race in progress</span>';
			} else {
				var ok = (state.seats || []).filter(function (r) {
					return r.status === 'ok';
				}).length;
				sum.innerHTML = '<span class="rh-gc-headmut">' + ok + '/' +
					(state.seats || []).length + ' calibrated</span>';
			}
		}

		var ctl = q('.rh-gc-ctl'); ctl.innerHTML = '';
		if (phase === 'running') {
			var stop = el('button', 'rh-gc-btn rh-gc-btn-stop', 'Stop');
			stop.addEventListener('click', function () { socket.emit('gate_cal_stop', {}); });
			ctl.appendChild(stop);
			ctl.appendChild(el('span', 'rh-gc-hint',
				'Carry each powered-up quad through the gate, over the timer.'));
		} else {
			var start = el('button', 'rh-gc-btn rh-gc-btn-start', '▶ Start calibration');
			var secsIn = el('input', 'rh-gc-secs');
			secsIn.type = 'number'; secsIn.min = 5; secsIn.max = 600; secsIn.step = 5;
			secsIn.value = state.secs || 30;
			secsIn.title = 'Calibration window duration, seconds';
			var secsLbl = el('span', 'rh-gc-secs-lbl', 's');
			start.addEventListener('click', function () {
				var v = parseInt(secsIn.value, 10);
				socket.emit('gate_cal_start', (v > 0 ? { secs: v } : {}));
			});
			ctl.appendChild(start);
			ctl.appendChild(secsIn);
			ctl.appendChild(secsLbl);
			if (state.adaptive_on && !state.priority_on) {
				ctl.appendChild(el('span', 'rh-gc-hint', chip('⚠ ADAPTIVE CALIBRATION IS ON',
					'rh-gc-c-adaptive',
					'RotorHazard’s Adaptive Calibration overwrites these walk-through ' +
					'thresholds every time a heat is selected, using values from previously ' +
					'saved races — your calibration will be lost before the race starts.\n\n' +
					'Either enable “Walk-through overrides Adaptive Calibration” in ' +
					'Settings → Gate Walkthrough Calibration, or switch ' +
					'Settings → Sensor Tuning → Calibration Mode to Manual.')));
			} else if (state.adaptive_on && state.priority_on) {
				ctl.appendChild(el('span', 'rh-gc-hint', chip('walk-through priority',
					'rh-gc-c-info',
					'Adaptive Calibration is enabled. A fresh walk-through calibration ' +
					'takes priority: it is re-applied after every heat change until the ' +
					'pilot races on that channel — then the newer race values win again.')));
			}
		}

		// countdown
		if (phase === 'running') {
			endsAt = Date.now() + (state.remaining || 0) * 1000;
			startTicker(); tick();
		} else {
			stopTicker();
			q('.rh-gc-timer').textContent = '';
			q('.rh-gc-bar').style.width = '0';
		}

		var rows = q('.rh-gc-rows'); rows.innerHTML = '';
		var seats = state.seats || [];
		// equal-width cells, always on a single row, stretched to fill the
		// panel exactly (no dead space when few pilots are seated); cells
		// compress on narrow screens
		rows.style.gridTemplateColumns =
			'repeat(' + Math.max(1, seats.length) + ', minmax(90px, 1fr))';
		seats.forEach(function (s2) { rows.appendChild(seatCell(s2)); });

		q('.rh-gc-foot').textContent = state.message || '';
	}

	function start() {
		if (!onRunPage()) {
			if (start._tries === undefined) { start._tries = 0; }
			if (start._tries++ < 40) { setTimeout(start, 250); }
			return;
		}
		ensureCss();
		socket = io.connect(location.protocol + '//' + document.domain + ':' + location.port);
		socket.on('connect', function () { socket.emit('gate_cal_get', {}); });
		socket.on('gate_cal_state', function (s) { render(s); });
		// RH broadcasts race_status on every race state change — including the
		// silent DONE→READY reset after "Save and Clear" (LAPS_SAVE fires while
		// the race is still DONE and nothing fires after) — refresh on it so
		// the panel unlocks without a page reload
		socket.on('race_status', function () { socket.emit('gate_cal_get', {}); });
		if (lightMq) {
			var onScheme = function () { if (theme === 'auto' && state.phase) render(state); };
			if (lightMq.addEventListener) lightMq.addEventListener('change', onScheme);
			else if (lightMq.addListener) lightMq.addListener(onScheme);
		}
		var tries = 0, iv = setInterval(function () {
			if (panel) { place(); }
			if (++tries > 20) { clearInterval(iv); }
		}, 500);
	}

	if (document.readyState === 'loading') {
		document.addEventListener('DOMContentLoaded', start);
	} else { start(); }
})();
