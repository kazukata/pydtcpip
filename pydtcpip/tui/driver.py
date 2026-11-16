"""Terminal driver that keeps mouse coordinates in character cells on POSIX terminals."""

from textual.drivers.linux_driver import LinuxDriver
from textual.message import Message
from textual.messages import InBandWindowResize


class CellMouseDriver(LinuxDriver):
    """
    Terminal driver that keeps mouse coordinates in character cells, as htop does.
    Textual switches to pixel coordinates when the terminal supports in-band resize notifications, and skips this
    for iTerm2 because of terminal bugs; over SSH iTerm2 cannot be recognized, so clicks land outside the screen.
    """

    def process_message(self, message: Message) -> None:
        # Leave in-band resize disabled so the terminal never switches to pixel mouse reports
        if isinstance(message, InBandWindowResize):
            return
        super().process_message(message)
