/**
 * chart.js
 * ========
 * TradingView Lightweight Charts integration.
 * Fetches OHLCV from backend /api/chart/:timeframe
 * and renders a candlestick chart with key levels and signal markers.
 */

const ChartEngine = (() => {
  const API = window.APP_CONFIG?.API_BASE || 'http://localhost:8000';

  let chart = null;
  let candleSeries = null;
  let volumeSeries = null;
  let keyLevelLines = [];
  let signalMarkers = [];
  let currentTF = '5m';

  function init(containerId) {
    const container = document.getElementById(containerId);
    if (!container) return;

    chart = LightweightCharts.createChart(container, {
      layout: {
        background: { color: '#0d1117' },
        textColor:  '#8b96a9',
      },
      grid: {
        vertLines: { color: 'rgba(255,255,255,0.04)' },
        horzLines: { color: 'rgba(255,255,255,0.04)' },
      },
      crosshair: {
        vertLine: { color: 'rgba(240,180,41,0.5)', labelBackgroundColor: '#1c2535' },
        horzLine: { color: 'rgba(240,180,41,0.5)', labelBackgroundColor: '#1c2535' },
      },
      rightPriceScale: {
        borderColor: 'rgba(255,255,255,0.06)',
        textColor: '#8b96a9',
        scaleMargins: { top: 0.1, bottom: 0.25 },
      },
      timeScale: {
        borderColor: 'rgba(255,255,255,0.06)',
        textColor: '#8b96a9',
        timeVisible: true,
        secondsVisible: false,
      },
      handleScroll: true,
      handleScale: true,
    });

    // Candlestick series
    candleSeries = chart.addCandlestickSeries({
      upColor:          '#22c55e',
      downColor:        '#ef4444',
      borderUpColor:    '#22c55e',
      borderDownColor:  '#ef4444',
      wickUpColor:      '#22c55e',
      wickDownColor:    '#ef4444',
    });

    // Volume series
    volumeSeries = chart.addHistogramSeries({
      color:          'rgba(240,180,41,0.15)',
      priceFormat:    { type: 'volume' },
      priceScaleId:   'volume',
      scaleMargins:   { top: 0.8, bottom: 0 },
    });

    chart.priceScale('volume').applyOptions({
      scaleMargins: { top: 0.8, bottom: 0 },
    });

    // Responsive resize
    const ro = new ResizeObserver(() => {
      if (chart && container) {
        chart.applyOptions({ width: container.clientWidth });
      }
    });
    ro.observe(container);

    loadChart(currentTF);
  }

  async function loadChart(tf) {
    currentTF = tf;
    const loading = document.getElementById('chart-loading');
    if (loading) loading.style.display = 'flex';

    try {
      const res  = await fetch(`${API}/api/chart/${tf}?bars=200`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();

      if (!data.bars || data.bars.length === 0) {
        throw new Error('No bar data returned');
      }

      const candles = data.bars.map(b => ({
        time:  b.time,
        open:  b.open,
        high:  b.high,
        low:   b.low,
        close: b.close,
      }));

      const volumes = data.bars.map(b => ({
        time:  b.time,
        value: b.volume,
        color: b.close >= b.open
          ? 'rgba(34,197,94,0.25)'
          : 'rgba(239,68,68,0.25)',
      }));

      candleSeries.setData(candles);
      volumeSeries.setData(volumes);
      chart.timeScale().fitContent();

    } catch (err) {
      console.error('[Chart] Load error:', err);
    } finally {
      if (loading) loading.style.display = 'none';
    }
  }

  function drawKeyLevels(levels) {
    // Remove old lines
    keyLevelLines.forEach(l => { try { candleSeries.removePriceLine(l); } catch(_) {} });
    keyLevelLines = [];

    levels.forEach(price => {
      const line = candleSeries.createPriceLine({
        price,
        color:       'rgba(240,180,41,0.45)',
        lineWidth:   1,
        lineStyle:   LightweightCharts.LineStyle.Dashed,
        axisLabelVisible: true,
        title:       `Key ${price.toFixed(2)}`,
      });
      keyLevelLines.push(line);
    });
  }

  function addSignalMarker(signal) {
    if (!candleSeries || !signal.entry_price) return;

    const marker = {
      time:     Math.floor(Date.now() / 1000),
      position: signal.direction === 'long' ? 'belowBar' : 'aboveBar',
      color:    signal.direction === 'long' ? '#22c55e' : '#ef4444',
      shape:    signal.direction === 'long' ? 'arrowUp' : 'arrowDown',
      text:     `${signal.signal_strength} ${signal.direction?.toUpperCase()}`,
    };
    signalMarkers.push(marker);
    candleSeries.setMarkers(signalMarkers);
  }

  return { init, loadChart, drawKeyLevels, addSignalMarker };
})();
