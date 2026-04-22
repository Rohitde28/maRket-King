/**
 * audio.js
 * ========
 * Web Audio API — alert sounds for signal events.
 * No external files needed. All tones generated synthetically.
 */

const AudioEngine = (() => {
  let ctx = null;

  function getCtx() {
    if (!ctx) {
      ctx = new (window.AudioContext || window.webkitAudioContext)();
    }
    // Resume if suspended (browser autoplay policy)
    if (ctx.state === 'suspended') ctx.resume();
    return ctx;
  }

  /** Generic tone builder */
  function tone(freq, duration, type = 'sine', gainVal = 0.3, startDelay = 0) {
    const ac  = getCtx();
    const osc = ac.createOscillator();
    const gn  = ac.createGain();
    osc.connect(gn);
    gn.connect(ac.destination);
    osc.frequency.setValueAtTime(freq, ac.currentTime + startDelay);
    osc.type = type;
    gn.gain.setValueAtTime(0, ac.currentTime + startDelay);
    gn.gain.linearRampToValueAtTime(gainVal, ac.currentTime + startDelay + 0.01);
    gn.gain.exponentialRampToValueAtTime(0.001, ac.currentTime + startDelay + duration);
    osc.start(ac.currentTime + startDelay);
    osc.stop(ac.currentTime + startDelay + duration + 0.05);
  }

  return {
    /**
     * ENTER signal — ascending 3-tone chime (positive, attention-grabbing)
     */
    playEnter() {
      tone(523.25, 0.18, 'sine', 0.35, 0.00);   // C5
      tone(659.25, 0.18, 'sine', 0.35, 0.15);   // E5
      tone(783.99, 0.35, 'sine', 0.40, 0.30);   // G5
    },

    /**
     * CAUTION signal — double mid-tone ping
     */
    playCaution() {
      tone(440, 0.15, 'triangle', 0.3, 0.00);   // A4
      tone(440, 0.20, 'triangle', 0.3, 0.20);   // A4 again
    },

    /**
     * No trade — soft low descend (subtle, not alarming)
     */
    playNoTrade() {
      tone(330, 0.25, 'sine', 0.15, 0.00);
    },

    /**
     * TP Hit — celebratory ascending arpeggio
     */
    playTPHit() {
      [523, 659, 784, 1047].forEach((f, i) => tone(f, 0.2, 'sine', 0.3, i * 0.12));
    },

    /**
     * SL Hit — low descending tone
     */
    playSLHit() {
      tone(330, 0.3, 'sawtooth', 0.2, 0.0);
      tone(220, 0.4, 'sawtooth', 0.15, 0.25);
    },

    /** Ensure AudioContext is ready (call on first user gesture) */
    unlock() {
      getCtx();
    }
  };
})();
