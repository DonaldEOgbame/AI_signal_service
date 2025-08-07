EMOJI = {
    'green': '🟢',
    'red': '🔴',
    'yellow': '🟡',
    'spark': '✨',
    'warning': '⚠️',
    'check': '✅',
    'news': '📰',
    'volatility': '🌪️'
}

def color_text(text: str, color: str) -> str:
    """Prefix text with a color emoji."""
    return f"{EMOJI.get(color, '')} {text}"

def confirm_warning() -> str:
    """Return formatted broker warning message with color icons."""
    return (
        f"{EMOJI['warning']}{EMOJI['yellow']} <b>AUTOMATED MODE ENABLED</b> {EMOJI['yellow']}{EMOJI['warning']}\n"
        "Please confirm your broker allows API trades…"
    )
