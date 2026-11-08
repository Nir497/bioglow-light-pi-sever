"""Conservative sound-source decisions from Perch's raw event logits."""
HUMAN_LABELS = frozenset('Speech Conversation Chatter Human_voice Whispering Shout Yell Screaming Singing Laughter Giggle Cough Sneeze Walk_and_footsteps'.split()) | {
    'Child_speech_and_kid_speaking', 'Female_speech_and_woman_speaking',
    'Male_speech_and_man_speaking', 'Female_singing', 'Male_singing',
    'Crying_and_sobbing', 'Chuckle_and_chortle', 'Cheering', 'Clapping', 'Applause'}
ANIMAL_LABELS = frozenset('Animal Bark Cat Dog Meow Purr Growling Chirp_and_tweet Chicken_and_rooster Cricket Crow Frog Fowl Insect'.split()) | {
    'Domestic_animals_and_pets', 'Livestock_and_farm_animals_and_working_animals',
    'Gull_and_seagull', 'Wild_animals'}


def classify_sound(scores, labels):
    from animal_model import is_species_label
    ranked = sorted(zip(labels, map(float, scores)), key=lambda item: -item[1])
    label, score = ranked[0]
    kind = lambda name: ('human' if name in HUMAN_LABELS else 'animal'
                         if name in ANIMAL_LABELS or is_species_label(name) else 'unknown')
    source = kind(label)
    competing = max((value for name, value in ranked if kind(name) != source), default=score)
    # Logits are not probabilities. Require positive evidence and separation;
    # mixed audio and environmental events remain unknown.
    if score <= 0 or score - competing < 1:
        source = 'unknown'
    return {'source': source, 'label': label, 'score': score,
            'margin': score - competing, 'method': 'perch-event-logits-v1'}
