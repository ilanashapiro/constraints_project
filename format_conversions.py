import mido
import pandas as pd
from midi2audio import FluidSynth
import os
from pydub import AudioSegment

def ticks_to_secs_with_tempo_changes(tick, tempo_changes, ticks_per_beat):
  # Calculate the time in seconds for a given tick considering all tempo changes.
  seconds = 0
  last_tick = 0
  for change_tick, tempo in tempo_changes:
    if tick < change_tick:
      break
    seconds += mido.tick2second(min(tick, change_tick) - last_tick, ticks_per_beat, tempo)
    last_tick = change_tick
  # Add remaining time if any tick after the last tempo change.
  if tick > last_tick:
    seconds += mido.tick2second(tick - last_tick, ticks_per_beat, tempo)
  return seconds

# CSV format from https://github.com/Wiilly07/Beethoven_motif 
def midi_to_csv():
  filename = "LOP_database_06_09_17/liszt_classical_archives/0_short_test/bl11_solo_short.mid" # for now
  output_filename = filename[:-4] + "_data.csv"

  print("Converting " + filename + " to " + output_filename)

  mid = mido.MidiFile(filename)
  df = pd.DataFrame(columns=["onset", "onset_seconds", "pitch", "duration", "staff"])

  ticks_per_beat = mid.ticks_per_beat
  microseconds_per_beat = 500000  # Default MIDI tempo is 500,000 microseconds per beat.

  # Process tempo changes first to simplify onset time calculation.
  tempo_changes = [(0, microseconds_per_beat)]  # (tick, tempo)
  for track in mid.tracks:
    current_tick = 0
    for msg in track:
      current_tick += msg.time
      if msg.type == 'set_tempo':
        tempo_changes.append((current_tick, msg.tempo))
  tempo_changes.sort(key=lambda x: x[0])
  
  def ticks_to_crochets(ticks, ticks_per_beat):
    return ticks / ticks_per_beat

  active_notes = {}  # Key: (note, channel), Value: onset_tick
  for track in mid.tracks:
    current_tick = 0
    for msg in track:
      current_tick += msg.time
      if msg.type == 'note_on' and msg.velocity > 0:
        active_notes[(msg.note, msg.channel)] = current_tick
      elif (msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0)) and (msg.note, msg.channel) in active_notes:
        onset_tick = active_notes.pop((msg.note, msg.channel))
        offset_tick = current_tick
        onset_seconds = ticks_to_secs_with_tempo_changes(onset_tick, tempo_changes, ticks_per_beat)
        onset_crochets = ticks_to_crochets(onset_tick, ticks_per_beat)
        duration_crochets = ticks_to_crochets(offset_tick - onset_tick, ticks_per_beat)
        staff = msg.channel + 1

        new_row = pd.DataFrame([[round(onset_crochets, 3), round(onset_seconds, 3), msg.note, round(duration_crochets, 3), staff]], columns=["onset", "onset_seconds", "pitch", "duration", "staff"]) 
        df = pd.concat([df, new_row], axis=0) 

  df.to_csv(output_filename, index=False) 
  print(f"Data has been written to {output_filename}")

# midi_to_csv()


def convert_dataset_midi_to_mp3():
	soundfont_filepath = "GeneralUser GS 1.471/GeneralUser GS v1.471.sf2"
	fs = FluidSynth(sound_font=soundfont_filepath)
	directory = "LOP_database_06_09_17/liszt_classical_archives/1_short_test"
	for root, _, files in os.walk(directory):
		for filename in files:
			if filename.endswith("_solo_short.mid"):
				midi_path = os.path.join(root, filename)
				wav_path = midi_path[:-4] + ".wav"
				fs.midi_to_audio(midi_path, wav_path)
				print("Converted", midi_path, "to WAV")

				mp3_path = midi_path[:-4] + ".mp3"
				audio = AudioSegment.from_wav(wav_path)
				audio.export(mp3_path, format="mp3", bitrate="192k", parameters=["-ar", "44100", "-ac", "2"])
				print("Converted", wav_path, "to MP3")

				os.remove(wav_path)
				print("Removed", wav_path)

convert_dataset_midi_to_mp3()
				
        