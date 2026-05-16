"""Video recorder — captures live MJPEG stream to file and converts to MP4."""
import threading, time, subprocess
from datetime import datetime
from pathlib import Path

from .config import VIDEOS_DIR

AUDIO_DEVICE = 'plughw:C930e,0'

def _check_audio_available() -> bool:
    """Return True if the ALSA device in AUDIO_DEVICE exists on this host."""
    try:
        card_name = AUDIO_DEVICE.split(':')[1].split(',')[0]
        cards = Path('/proc/asound/cards').read_text()
        return card_name.lower() in cards.lower()
    except Exception:
        return False

def _get_pulse_source() -> str:
    """Return the PulseAudio/PipeWire source name matching AUDIO_DEVICE.

    PipeWire runs as the system sound server and holds the ALSA device
    exclusively.  Using the PulseAudio interface (which PipeWire exposes)
    allows multiple simultaneous readers without 'device busy' errors.
    """
    try:
        card_name = AUDIO_DEVICE.split(':')[1].split(',')[0].lower()
        result = subprocess.run(
            ['pactl', 'list', 'sources', 'short'],
            capture_output=True, text=True, timeout=3,
        )
        for line in result.stdout.splitlines():
            parts = line.split('\t')
            if len(parts) >= 2 and card_name in parts[1].lower():
                return parts[1]
    except Exception:
        pass
    return 'default'

AUDIO_AVAILABLE: bool = _check_audio_available()
PULSE_SOURCE:    str  = _get_pulse_source() if AUDIO_AVAILABLE else 'default'


def _extract_thumbnail(mp4_path: Path) -> None:
    """Extract a single frame from 10 % into the video as a JPEG thumbnail."""
    thumb = mp4_path.with_suffix(".thumb.jpg")
    try:
        subprocess.run(
            ["ffmpeg", "-y",
             "-ss", "0.1",           # start slightly in so black frames are avoided
             "-i", str(mp4_path),
             "-vf", "thumbnail=100", # pick best frame from first 100
             "-frames:v", "1",
             "-q:v", "3",            # JPEG quality (2=best, 5=good)
             str(thumb)],
            capture_output=True, timeout=30,
        )
    except Exception:
        pass


def _convert_recording(src, dst, fps, crf=23, audio_src=None, start_ts=None):
    """Convert a raw MJPEG dump to H.264 MP4; delete source on success."""
    cmd = ['ffmpeg', '-y', '-r', str(fps), '-f', 'mjpeg', '-i', str(src)]
    if audio_src and Path(audio_src).exists() and Path(audio_src).stat().st_size > 0:
        # Audio filter chain — tuned for webcam voice capture (order matters):
        #
        #   highpass=f=100       — cut desk vibration / HVAC rumble below 100 Hz;
        #                          male voice fundamental starts ~85 Hz so nothing
        #                          useful is lost above 100 Hz
        #
        #   lowpass=f=10000      — webcam mics have high self-noise above ~10 kHz;
        #                          cutting here removes that hiss while keeping full
        #                          voice presence and sibilance (1 kHz–8 kHz range)
        #
        #   afftdn=nf=-25        — FFT noise reduction at −25 dB floor; removes
        #                          constant fan/background noise without introducing
        #                          musical-noise artefacts
        #
        #   acompressor          — gentle voice compression:
        #                            threshold=-18dB  starts compressing at a
        #                                             comfortable speech level
        #                            ratio=2.5        subtle, not "radio" sounding
        #                            attack=15ms      slow enough to pass natural
        #                                             consonant transients unaltered
        #                            release=250ms    long enough to avoid pumping
        #                                             between syllables
        #                            makeup=1         no automatic gain boost
        #
        #   loudnorm=I=-18       — −18 LUFS target; louder and more intelligible
        #                          than the −23 broadcast standard — better for
        #                          outdoor/ambient monitoring where you want to
        #                          actually hear what is said
        #            TP=-1.5     — true-peak ceiling keeps single-pass mode safely
        #                          below 0 dBFS
        #            LRA=9       — tighter loudness range (9 LU) evens out the
        #                          difference between near and far voices
        #
        #   alimiter             — brick-wall safety net: catches anything that
        #                          slips through loudnorm's single-pass estimation
        audio_filters = (
            "highpass=f=100,"
            "lowpass=f=10000,"
            "afftdn=nf=-25,"
            "acompressor=threshold=-18dB:ratio=2.5:attack=15:release=250:makeup=1,"
            "loudnorm=I=-18:TP=-1.5:LRA=9,"
            "alimiter=level_in=1:level_out=1:limit=0.9:attack=5:release=50"
        )
        cmd += ['-i', str(audio_src),
                '-af', audio_filters,
                '-c:a', 'aac', '-b:a', '192k', '-ar', '48000', '-ac', '1',
                '-shortest']
    cmd += ['-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', str(crf)]
    if start_ts:
        # start_ts format: "YYYY-MM-DD_HH-MM-SS" → ISO 8601 for ffmpeg
        iso_dt = start_ts[:10] + "T" + start_ts[11:].replace("-", ":")
        cmd += [
            '-metadata', f'creation_time={iso_dt}',
            '-metadata', f'title={dst.stem}',
            '-metadata', 'comment=Home Garden Cameras Video',
            '-metadata', 'encoder=Home Garden Cameras',
        ]
    cmd += [str(dst)]
    result = subprocess.run(cmd, capture_output=True, timeout=300)
    if result.returncode == 0:
        src.unlink(missing_ok=True)
        if audio_src:
            Path(audio_src).unlink(missing_ok=True)
        _extract_thumbnail(dst)


class VideoRecorder:
    """Records the live MJPEG stream to a .mjpeg file then converts to MP4."""

    def __init__(self):
        self._lock = threading.Lock()
        self.running = False
        self._file = None
        self.filename = None
        self.start_time = None
        self.frame_count = 0
        self._audio_proc = None
        self._audio_file = None
        self._audio_requested = False

    def start(self, crf=23, audio=False):
        with self._lock:
            if self.running:
                return False
            ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            self.filename = f"Video_{ts}.mjpeg"
            self._file = open(VIDEOS_DIR / self.filename, "wb")
            self.frame_count = 0
            self.start_time = time.time()
            self._start_ts  = ts
            self.running = True
            self.crf = crf
            self._audio_proc = None
            self._audio_file = None
            self._audio_requested = bool(audio)
            if audio:
                audio_path = VIDEOS_DIR / f"Video_{ts}.wav"
                self._audio_file = str(audio_path)
                try:
                    self._audio_proc = subprocess.Popen(
                        ['ffmpeg', '-y',
                         '-f', 'pulse', '-ar', '48000', '-ac', '2',
                         '-i', PULSE_SOURCE,
                         str(audio_path)],
                        stderr=subprocess.PIPE, stdout=subprocess.DEVNULL,
                    )
                except Exception:
                    self._audio_proc = None
                    self._audio_file = None
        return True

    def write(self, frame):
        with self._lock:
            if self.running and self._file:
                self._file.write(frame)
                self.frame_count += 1

    def stop(self):
        with self._lock:
            if not self.running:
                return None, False
            self.running = False
            duration          = time.time() - self.start_time if self.start_time else 0
            fc                = self.frame_count
            fname             = self.filename
            start_ts          = getattr(self, "_start_ts", None)
            audio_proc        = self._audio_proc
            audio_file        = self._audio_file
            audio_requested   = self._audio_requested
            self._audio_proc      = None
            self._audio_file      = None
            self._audio_requested = False
            if self._file:
                self._file.close()
                self._file = None
        if audio_proc:
            audio_proc.terminate()
            try:
                audio_proc.wait(timeout=5)
            except Exception:
                audio_proc.kill()
                try:
                    audio_proc.wait(timeout=2)
                except Exception:
                    pass

        # Confirm audio actually captured something
        audio_ok = False
        if audio_requested and audio_file:
            p = Path(audio_file)
            audio_ok = p.exists() and p.stat().st_size > 4096

        if fname and fc > 0 and duration > 0:
            fps = max(1, round(fc / duration))
            src = VIDEOS_DIR / fname
            dst = VIDEOS_DIR / fname.replace(".mjpeg", ".mp4")
            threading.Thread(
                target=_convert_recording,
                args=(src, dst, fps, self.crf, audio_file if audio_ok else None, start_ts),
                daemon=True,
            ).start()
        return fname, audio_ok

    def status(self):
        with self._lock:
            return {
                "running":     self.running,
                "filename":    self.filename,
                "duration":    round(time.time() - self.start_time, 1)
                               if self.start_time and self.running else 0,
                "frame_count": self.frame_count,
            }


video_recorder = VideoRecorder()


class AudioStreamer:
    """Streams live audio from the ALSA device to HTTP clients in real time.

    Each subscriber spawns its own ffmpeg process so that it receives a
    complete Ogg stream (including headers) from the start.  On most Linux
    systems USB audio devices allow concurrent readers; if the hardware is
    exclusive the stream simply fails to start without crashing.
    """

    def subscribe_aac(self):
        """Generator that yields AAC/ADTS chunks for the Safari <audio> fallback.

        ADTS is a self-synchronising framing format: each frame carries its own
        header, so the browser can start decoding mid-stream without needing the
        beginning of the file.  Safari has native AAC support (Apple's own codec),
        making this the only format guaranteed to work in Safari's <audio> element.
        """
        if not AUDIO_AVAILABLE:
            return
        try:
            proc = subprocess.Popen(
                ['ffmpeg', '-y',
                 '-f', 'pulse', '-fragment_size', '4096', '-i', PULSE_SOURCE,
                 '-af', (
                     'highpass=f=80,'
                     'alimiter=level_in=1:level_out=1:limit=0.9:attack=3:release=25'
                 ),
                 '-c:a', 'aac', '-b:a', '64k',
                 '-flush_packets', '1',   # flush every encoded AAC frame immediately
                 '-f', 'adts',
                 'pipe:1'],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            return
        try:
            while True:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                yield chunk
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:
                proc.kill()

    def subscribe_raw(self):
        """Generator that yields raw s16le PCM chunks for Web Audio API playback.

        16 kHz mono, signed 16-bit little-endian.  No container overhead means
        data flows as soon as PulseAudio produces it.  Reads in 512-byte
        blocks (256 samples = 16 ms) for minimal scheduling jitter.
        """
        if not AUDIO_AVAILABLE:
            return
        try:
            proc = subprocess.Popen(
                ['ffmpeg', '-y',
                 '-f', 'pulse', '-fragment_size', '4096', '-i', PULSE_SOURCE,
                 '-af', (
                     'highpass=f=80,'
                     'alimiter=level_in=1:level_out=1:limit=0.9:attack=3:release=25'
                 ),
                 '-ar', '16000', '-ac', '1',   # output-side resample — always 16 kHz mono
                 '-f', 's16le',
                 'pipe:1'],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            return
        try:
            while True:
                chunk = proc.stdout.read(512)   # 256 samples ≈ 16 ms
                if not chunk:
                    break
                yield chunk
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:
                proc.kill()


audio_streamer = AudioStreamer()
