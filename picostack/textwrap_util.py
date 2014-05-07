import textwrap


def wrap_multiline(text):
    compact_lines = [line.strip() for line in text.split('\n')]
    compacted_text = '\n'.join(compact_lines)
    command_lines = textwrap.wrap(compacted_text, width=210,
                                  break_on_hyphens=False,
                                  break_long_words=False)
    return '\n'.join(command_lines).strip()
