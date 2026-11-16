"""Dialogs for confirming Moves, handling failures, choosing resources and adding devices."""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Literal

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, OptionList, Static
from textual.widgets.option_list import Option


type ButtonVariant = Literal['default', 'primary', 'success', 'warning', 'error']


@dataclass
class ConfirmEntry:
    """One recording as presented in the Move confirmation."""

    title: str
    quality: str
    size: str
    copies: Text
    output_name: str
    # Warnings as short labels, explanations and Rich styles, such as deletion of the source recording
    warnings: list[tuple[str, str, str]] = field(default_factory=list[tuple[str, str, str]])
    # Reason the recording will not be moved, or None when it will be
    excluded: str | None = None


def _expand_directory(value: str) -> Path | None:
    """
    Expand a destination typed by the user.

    Args:
        value (str): Directory text, possibly starting with ~ or ~user

    Returns:
        Path | None: Expanded directory, or None when the home directory of ~user cannot be determined
    """

    try:
        return Path(value).expanduser()
    except RuntimeError:
        return None


class ChoiceScreen(ModalScreen[str | None]):
    """A message with a set of buttons; Escape dismisses without a choice."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding('escape', 'dismiss_choice', 'Back')]

    def __init__(
        self,
        title: str,
        body: list[tuple[str, str]],
        choices: list[tuple[str, str, ButtonVariant]],
        *,
        default: str | None = None,
    ) -> None:
        """
        Configure the message and buttons.

        Args:
            title (str): Dialog title
            body (list[tuple[str, str]]): Paragraphs with Rich styles
            choices (list[tuple[str, str, ButtonVariant]]): Button IDs, labels and variants
            default (str | None): Button focused initially (the first if None)
        """

        super().__init__()
        self._title = title
        self._body = body
        self._choices = choices
        self._default = default if default is not None else choices[0][0]

    def compose(self) -> ComposeResult:
        with Vertical(classes='dialog'):
            yield Label(self._title, classes='dialog-title')
            for text, style in self._body:
                yield Static(Text(text, style=style), classes='dialog-text')
            with Horizontal(classes='dialog-buttons'):
                for choice_id, label, variant in self._choices:
                    yield Button(label, id=f'choice-{choice_id}', variant=variant)

    def on_mount(self) -> None:
        self.query_one(f'#choice-{self._default}', Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id is not None:
            self.dismiss(event.button.id.removeprefix('choice-'))

    def action_dismiss_choice(self) -> None:
        """Close the dialog without choosing."""

        self.dismiss(None)


class ConfirmMoveScreen(ModalScreen[Path | None]):
    """Confirm the recordings, warnings and destination before consuming copies on the source."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding('escape', 'cancel', 'Cancel')]

    def __init__(
        self,
        compute: Callable[[Path], list[ConfirmEntry]],
        directory: Path,
        notes: list[tuple[str, str]],
    ) -> None:
        """
        Configure the confirmation.

        Args:
            compute (Callable[[Path], list[ConfirmEntry]]): Builds entries, destinations and exclusions for a directory
            directory (Path): Initial destination directory
            notes (list[tuple[str, str]]): Notes applying to every recording, with Rich styles
        """

        super().__init__()
        self._compute = compute
        self._directory = directory
        self._notes = notes
        self._entries: list[ConfirmEntry] = []

    def compose(self) -> ComposeResult:
        with Vertical(classes='dialog wide'):
            yield Label('Move recordings', classes='dialog-title')
            yield Static(
                Text(
                    'Each Move consumes one copy on the source. If only the final Move remains or copying is '
                    'prohibited, the source recording is deleted.',
                    style='bold',
                ),
                classes='dialog-text',
            )
            for text, style in self._notes:
                yield Static(Text(text, style=style), classes='dialog-text')
            yield Label('Destination directory')
            yield Input(str(self._directory), id='confirm-directory')
            table: DataTable[Text | str] = DataTable(id='confirm-table', cursor_type='row', zebra_stripes=True)
            yield table
            yield Static(id='confirm-warnings', classes='dialog-text')
            with Horizontal(classes='dialog-buttons'):
                yield Button('Cancel', id='confirm-cancel')
                yield Button('Move', id='confirm-move', variant='error')

    def on_mount(self) -> None:
        table = self.query_one('#confirm-table', DataTable)
        table.add_column('Title', width=40)
        table.add_column('Quality', width=24)
        table.add_column('Size', width=10)
        table.add_column('Copies', width=9)
        table.add_column('Warnings', width=28)
        table.add_column('Saved as')
        self._refresh(self._directory)
        self.query_one('#confirm-cancel', Button).focus()

    def _refresh(self, directory: Path) -> None:
        """
        Recompute entries for a destination and update the table, warnings and button label.

        Args:
            directory (Path): Destination directory
        """

        self._directory = directory
        self._entries = self._compute(directory)
        table: DataTable[Text | str] = self.query_one('#confirm-table', DataTable)
        table.clear()
        explanations: dict[str, tuple[str, str, int]] = {}
        for entry in self._entries:
            saved_as = Text(f'Excluded: {entry.excluded}', style='yellow') if entry.excluded else entry.output_name
            labels = Text(', ').join(Text(label, style=style) for label, _, style in entry.warnings)
            table.add_row(entry.title, entry.quality, entry.size, entry.copies, labels, saved_as)
            if entry.excluded is not None:
                continue
            for label, explanation, style in entry.warnings:
                _, _, count = explanations.get(label, (explanation, style, 0))
                explanations[label] = (explanation, style, count + 1)
        legend = Text()
        for label, (explanation, style, count) in explanations.items():
            legend.append(f'{label} ({count}): ', style=f'bold {style}')
            legend.append(f'{explanation}\n', style=style)
        self.query_one('#confirm-warnings', Static).update(legend)
        count = sum(1 for entry in self._entries if entry.excluded is None)
        button = self.query_one('#confirm-move', Button)
        button.label = f'Move {count} recording' + ('' if count == 1 else 's')
        button.disabled = count == 0

    @on(Input.Changed, '#confirm-directory')
    def _directory_changed(self, event: Input.Changed) -> None:
        # Recompute destinations and exclusions while typing so the Move button reflects the new directory
        directory = _expand_directory(event.value)
        if directory is None:
            # A partially typed ~user cannot be expanded yet
            self.query_one('#confirm-move', Button).disabled = True
            return
        self._refresh(directory)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == 'confirm-cancel':
            self.dismiss(None)
            return
        value = self.query_one('#confirm-directory', Input).value
        directory = _expand_directory(value)
        if directory is None:
            self.notify(f'Could not resolve the home directory in {value}', severity='error')
            return
        if directory.is_dir() is False:
            self.notify(f'Destination directory not found: {directory}', severity='error')
            return
        self.dismiss(directory)

    def action_cancel(self) -> None:
        """Close the confirmation without moving."""

        self.dismiss(None)


class ResourceScreen(ModalScreen[int | None]):
    """Choose the resource to transfer; 0 restores the default original recording."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding('escape', 'cancel', 'Cancel')]

    def __init__(self, title: str, options: list[tuple[int, str, bool]]) -> None:
        """
        Configure the resource list.

        Args:
            title (str): Recording title
            options (list[tuple[int, str, bool]]): One-based resource numbers, labels and whether each can be moved
        """

        super().__init__()
        self._title = title
        self._options = options

    def compose(self) -> ComposeResult:
        with Vertical(classes='dialog wide'):
            yield Label(f'Quality to move: {self._title}', classes='dialog-title')
            yield Static(
                Text(
                    'The original recording is moved by default. Other versions may have lower quality, '
                    'and moving them still consumes a copy of the source recording.',
                    style='yellow',
                ),
                classes='dialog-text',
            )
            options = [Option('Default: original recording', id='resource-0')]
            options.extend(
                Option(label, id=f'resource-{index}', disabled=not is_available)
                for index, label, is_available in self._options
            )
            yield OptionList(*options, id='resource-list')

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id is not None:
            self.dismiss(int(event.option.id.removeprefix('resource-')))

    def action_cancel(self) -> None:
        """Close without changing the resource."""

        self.dismiss(None)


class AddDeviceScreen(ModalScreen[str | None]):
    """Enter the device description URL of a recorder that discovery does not find."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding('escape', 'cancel', 'Cancel')]

    def compose(self) -> ComposeResult:
        with Vertical(classes='dialog'):
            yield Label('Add a recorder', classes='dialog-title')
            yield Static(
                'Enter the device description URL shown by `pydtcpip discover` or the device.', classes='dialog-text'
            )
            yield Input(placeholder='http://192.168.1.10:5000/description.xml', id='device-url')

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        self.dismiss(value or None)

    def action_cancel(self) -> None:
        """Close without adding a device."""

        self.dismiss(None)
