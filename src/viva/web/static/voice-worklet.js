// AudioWorkletProcessor that buffers raw Float32 PCM samples and posts
// them to the main thread in ~0.1s chunks, rather than every 128-sample
// render quantum (docs/system-design/17-phase-12-web-voice-io-design.md
// \u00a717.6) -- posting every quantum would be thousands of postMessage
// calls per second for no benefit; the main thread's energy-based
// silence check only needs roughly the same block granularity the CLI's
// own _record_pcm() already uses (docs/system-design/
// 16-phase-11-voice-io-design.md \u00a716.3's 0.1s blocks).
//
// Runs at the AudioContext's native sample rate (44.1kHz/48kHz
// depending on the machine) -- resampling to the 16kHz faster-whisper
// expects happens on the main thread, after recording stops, via
// OfflineAudioContext (app.js).
class VoiceCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._chunks = [];
    this._bufferedSamples = 0;
    this._blockSamples = Math.round(sampleRate * 0.1);
  }

  process(inputs) {
    const input = inputs[0];
    if (input && input[0] && input[0].length > 0) {
      // slice(): the Float32Array passed in is reused by the audio
      // pipeline across calls, so it must be copied before buffering.
      this._chunks.push(input[0].slice());
      this._bufferedSamples += input[0].length;
      if (this._bufferedSamples >= this._blockSamples) {
        const merged = new Float32Array(this._bufferedSamples);
        let offset = 0;
        for (const chunk of this._chunks) {
          merged.set(chunk, offset);
          offset += chunk.length;
        }
        this.port.postMessage(merged, [merged.buffer]);
        this._chunks = [];
        this._bufferedSamples = 0;
      }
    }
    return true; // keep the processor alive for the life of the recording
  }
}

registerProcessor("voice-capture-processor", VoiceCaptureProcessor);
