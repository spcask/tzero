#!/usr/bin/env python3

"""IRC Channel Timebox Keeper."""


import json
import logging
import pathlib
import select
import socket
import ssl
import time

_NAME = 'tzero'
_log = logging.getLogger(_NAME)


class _ctx:
    retry_delay = 1
    state = {
        'timebox': {}
    }
    commands = {}


def main():
    log_fmt = ('%(asctime)s %(levelname)s %(filename)s:%(lineno)d '
               '%(funcName)s() %(message)s')
    logging.basicConfig(format=log_fmt, level=logging.INFO)

    # Read configuration.
    with open(f'{_NAME}.json', encoding='utf-8') as stream:
        config = json.load(stream)

    # Ensure we can write to state file.
    _read_state(config['state'])
    _write_state(config['state'])

    # Run application forever.
    _set_up_commands()
    while True:
        try:
            _run(config['host'], config['port'], config['tls'],
                 config['nick'], config['password'], config['channels'],
                 config['prefix'], config['block'], config['state'])
        except Exception:
            _log.exception('Client encountered error')
            _log.info('Reconnecting in %d s', _ctx.retry_delay)
            time.sleep(_ctx.retry_delay)
            _ctx.retry_delay = min(_ctx.retry_delay * 2, 3600)


def _run(host, port, tls,
         nick, password, channels,
         prefix, blocked_words, state_filename):
    _log.info('Connecting ...')
    sock = socket.create_connection((host, port))
    if tls:
        tls_context = ssl.create_default_context()
        sock = tls_context.wrap_socket(sock, server_hostname=host)

    _log.info('Authenticating ...')
    _send(sock, f'PASS {password}')
    _send(sock, f'NICK {nick}')
    _send(sock, f'USER {nick} {nick} {host} :{nick}')

    _log.info('Joining channels ...')
    for channel in channels:
        _send(sock, f'JOIN {channel}')

    _log.info('Receiving messages ...')
    for line in _recv(sock):
        if line is not None:
            sender, command, middle, trailing = _parse_line(line)
            if command == 'PING':
                _send(sock, f'PONG :{trailing}')
                _ctx.retry_delay = 1
            elif command == 'PRIVMSG':
                _log.info(
                    'sender: %s; command: %s; middle: %s; trailing: %s',
                    sender, command, middle, trailing)
                if (sender and middle and trailing and
                        trailing.startswith(prefix)):
                    try:
                        _process_command(sock, nick, prefix, blocked_words,
                                         sender, middle, trailing)
                    except Exception:
                        _log.exception('Command processor encountered error')
        try:
            _complete_timeboxes(sock)
            _clean_timeboxes()
        except Exception:
            _log.exception('Task processor encountered error')
        _write_state(state_filename)


def _set_up_commands():
    _ctx.commands = {
        'begin': {
            'private': False,
            'public': True,
            'action': _begin_timebox,
            'help': _begin_timebox_help,
        },
        'cancel': {
            'private': False,
            'public': True,
            'action': _cancel_timebox,
            'help': _cancel_timebox_help,
        },
        'delete': {
            'private': False,
            'public': True,
            'action': _delete_timebox,
            'help': _delete_timebox_help,
        },
        'help': {
            'private': True,
            'public': True,
            'action': _help,
            'help': _help_help,
        },
        'list': {
            'private': True,
            'public': True,
            'action': _list_completed_timeboxes,
            'help': _list_completed_timeboxes_help,
        },
        'running': {
            'private': True,
            'public': True,
            'action': _list_running_timeboxes,
            'help': _list_running_timeboxes_help,
        },
        'time': {
            'private': True,
            'public': True,
            'action': _current_time,
            'help': _current_time_help,
        },
    }


def _process_command(sock, nick, prefix, blocked_words,
                     sender, recipient, message):
    # If this tool's nickname is same as the receiver name (recipient)
    # found in the received message, the message was sent privately to
    # this tool.
    private = nick == recipient

    # While replying to private messages, the response should be sent
    # to the sender.  While replying to channel messages, the response
    # should be sent to the channel (recipient).
    reply_to = sender if private else recipient

    words = message.split()
    command = _remove_prefix(words[0], prefix)
    params = words[1:]

    # Validate command.
    matches = _find_command(command)
    if len(matches) == 0:
        msg = ('Error: Unrecognized command.  Available commands: ' +
               _command_list(prefix, _ctx.commands) + '.')
        _send_message(sock, reply_to, msg)
        return

    if len(matches) > 1:
        msg = ('Error: Ambiguous command.  Matching commands: ' +
               _command_list(prefix, matches) + '.')
        _send_message(sock, reply_to, msg)
        return

    command = matches[0]
    decl = _ctx.commands[command]
    if private and not decl['private']:
        msg = 'Error: This command must be sent in channel.'
        _send_message(sock, reply_to, msg)
    elif not private and not decl['public']:
        msg = 'Error: This command must be sent in private.'
        _send_message(sock, reply_to, msg)
    elif any(word in params for word in blocked_words):
        msg = 'Error: Parameters contain blocked word.'
        _send_message(sock, reply_to, msg)
    else:
        action_func = decl['action']
        throttle_delay = 0
        for msg in action_func(prefix, sender, command, params, reply_to):
            _send_message(sock, reply_to, msg)
            time.sleep(throttle_delay)
            throttle_delay = 1


# Command ,begin
def _begin_timebox(prefix, sender, command, params, reply_to):
    if len(params) == 0:
        return ['Error: ' + _begin_timebox_help(prefix, command)[0]]

    if params[0].isdigit():
        if len(params) == 1:
            return ['Error: Duration must be followed by task summary.']
        duration = int(params[0])
        summary = ' '.join(params[1:])
    else:
        duration = 30
        summary = ' '.join(params)

    if duration < 15:
        return ['Error: Duration must be at least 15 minutes.']

    if duration > 60:
        return ['Error: Duration must not exceed 60 minutes.']

    if duration % 5 != 0:
        return ['Error: Duration must be a multiple of 5 minutes.']

    timeboxes = _ctx.state['timebox'].get(sender)
    if timeboxes is not None and not timeboxes[-1]['completed']:
        return ['Error: Another timebox is in progress: ' +
                _format_timebox(timeboxes[-1]) + '.  ' +
                'Send ,cancel to cancel the currently running timebox before '
                'starting a new timebox.']

    if timeboxes is None:
        timeboxes = _ctx.state['timebox'][sender] = []

    timeboxes.append({
        'person': sender,
        'reply_to': reply_to,
        'start': int(time.time()),
        'duration': duration,
        'summary': summary,
        'completed': False,
    })
    return ['Started timebox: ' + _format_timebox(timeboxes[-1])]


def _begin_timebox_help(prefix, command):
    return [
        f'Usage: {prefix}{command} [MINUTES] SUMMARY.  '
        f'Example #1: {prefix}{command} Write new blog post.  '
        f'Example #2: {prefix}{command} 45 Review article.  '
        'Start a new timebox for the specified number of MINUTES.  '
        'MINUTES must be a multiple of 5 between 15 and 60, inclusive.  '
        'If MINUTES is not specified, default to 30 minutes.'
    ]


# Command ,cancel
def _cancel_timebox(prefix, sender, command, params, _reply_to):
    if len(params) > 0:
        return ['Error: ' + _cancel_timebox_help(prefix, command)[0]]

    timeboxes = _ctx.state['timebox'].get(sender)
    if timeboxes is None or timeboxes[-1]['completed']:
        return [f'Error: No running timeboxes found for {sender}.']

    cancelled_timebox = timeboxes[-1]
    del timeboxes[-1]
    if len(timeboxes) == 0:
        del _ctx.state['timebox'][sender]
    return ['Cancelled running timebox: ' + _format_timebox(cancelled_timebox)]


def _cancel_timebox_help(prefix, command):
    return [f'Usage: {prefix}{command}.  '
            'Cancel your currently running timebox.']


# Command ,delete
def _delete_timebox(prefix, sender, command, params, _reply_to):
    if len(params) > 0:
        return ['Error: ' + _delete_timebox_help(prefix, command)[0]]

    timeboxes = _ctx.state['timebox'].get(sender)
    if timeboxes is None:
        return [f'Error: No timeboxes found for {sender}.']

    if not timeboxes[-1]['completed']:
        return ['Warning: Another timebox is in progress: ' +
                _format_timebox(timeboxes[-1]) + '.  ' +
                'First cancel the running timebox with ,cancel.  ' +
                'Then delete the last completed timebox with ,delete.']

    deleted_timebox = timeboxes[-1]
    del timeboxes[-1]
    if len(timeboxes) == 0:
        del _ctx.state['timebox'][sender]
    return ['Deleted the last completed timebox: ' +
            _format_timebox(deleted_timebox)]


def _delete_timebox_help(prefix, command):
    return [f'Usage: {prefix}{command}.  Delete your last completed timebox.']


# Command ,help
def _help(prefix, _sender, command, params, _reply_to):
    if len(params) == 0:
        return _help_help(prefix, command)

    params[0] = _remove_prefix(params[0], prefix)
    matches = _find_command(params[0])

    if len(matches) == 0:
        return ['Error: Unrecognized command.  Available commands: ' +
                _command_list(prefix, _ctx.commands) + '.']

    if len(matches) > 1:
        return ['Error: Ambiguous command.  Matching commands: ' +
                _command_list(prefix, matches) + '.']

    cmd = matches[0]
    return _ctx.commands[cmd]['help'](prefix, cmd)


def _help_help(prefix, command):
    return [f'Usage: {prefix}{command} [COMMAND].  Available commands: ' +
            _command_list(prefix, _ctx.commands) + '.']


# Command ,list
def _list_completed_timeboxes(prefix, sender, command, params, _reply_to):
    if len(params) > 0:
        return ['Error: ' + _list_completed_timeboxes_help(prefix, command)[0]]

    timeboxes = _ctx.state['timebox'].get(sender)
    if timeboxes is None:
        return [f'No timeboxes found for {sender}.']

    completed = [t for t in timeboxes if t['completed']]
    if len(completed) == 0:
        return [f'No completed timeboxes found for {sender}.']

    completed.sort(key=lambda x: x['start'], reverse=True)
    return [_format_timebox(t) for t in completed]


def _list_completed_timeboxes_help(prefix, command):
    return [
        f'Usage: {prefix}{command}.  List your completed timeboxes.  '
        'Only your most recent 10 timeboxes started within the last 48 hours '
        'are available.  '
        'Older timeboxes are permanently removed from the system.'
    ]


# Command ,running
def _list_running_timeboxes(prefix, _sender, command, params, reply_to):
    if len(params) > 0:
        return ['Error: ' + _list_running_timeboxes_help(prefix, command)[0]]

    running = []
    for timeboxes in _ctx.state['timebox'].values():
        if (timeboxes[-1]['reply_to'] == reply_to and
                not timeboxes[-1]['completed']):
            running.append(timeboxes[-1])

    if len(running) == 0:
        return [f'No running timeboxes found for {reply_to}.']

    running.sort(key=lambda x: x['start'], reverse=True)
    return [_format_timebox(t) for t in running]


def _list_running_timeboxes_help(prefix, command):
    return [f'Usage: {prefix}{command}.  '
            'List all running timeboxes of the channel.']


# Command ,time
def _current_time(prefix, _sender, command, params, _reply_to):
    if len(params) > 0:
        return ['Error: ' + _current_time_help(prefix, command)[0]]
    return [time.strftime('%Y-%m-%d %H:%M:%S %Z', time.gmtime())]


def _current_time_help(prefix, command):
    return [f'Usage: {prefix}{command}.  Show current UTC time.']


# Tasks.
def _complete_timeboxes(sock):
    current_time = int(time.time())
    for timeboxes in _ctx.state['timebox'].values():
        last = timeboxes[-1]
        if (not last['completed'] and
                last['start'] + last['duration'] * 60 <= current_time):
            last['completed'] = True
            msg = 'Completed timebox: ' + _format_timebox(last)
            _send_message(sock, last['reply_to'], msg)


def _clean_timeboxes():
    current_time = int(time.time())
    cleaned_state_timebox = {}
    max_timeboxes = 10
    for person, timeboxes in _ctx.state['timebox'].items():
        cleaned_timeboxes = []
        for timebox in timeboxes:
            if current_time <= timebox['start'] + 2 * 86400:
                cleaned_timeboxes.append(timebox)
        cleaned_timeboxes = cleaned_timeboxes[-max_timeboxes:]
        if len(cleaned_timeboxes) > 0:
            cleaned_state_timebox[person] = cleaned_timeboxes
    _ctx.state['timebox'] = cleaned_state_timebox


# Utility functions
def _read_state(filename):
    if pathlib.Path(filename).exists():
        with open(filename, encoding='utf-8') as stream:
            _ctx.state = json.load(stream)


def _write_state(filename):
    with open(filename, 'w', encoding='utf-8') as stream:
        json.dump(_ctx.state, stream, indent=2)


def _find_command(command):
    return [c for c in _ctx.commands if c.startswith(command)]


def _command_list(prefix, commands):
    return ' '.join(prefix + c for c in commands)


def _remove_prefix(word, prefix):
    if word.startswith(prefix):
        return word[len(prefix):]
    return word


def _format_timebox(timebox):
    person = timebox['person']
    start = timebox['start']
    duration = timebox['duration']
    summary = timebox['summary']
    start_str = time.strftime('%H:%M %Z', time.gmtime(start))
    return f'{person} [{start_str}] ({duration} min) {summary}'


# Protocol functions
def _recv(sock):
    buffer = ''
    while True:
        # Check if any data has been received.
        rlist, _, _ = select.select([sock], [], [], 1)
        if len(rlist) == 0:
            yield None
            continue

        # If data has been received, validate data length.
        data = sock.recv(1024)
        if len(data) == 0:
            message = 'Received zero-length payload from server'
            _log.error(message)
            raise ValueError(message)

        # If there is nonempty data, yield lines from it.
        buffer += data.decode(errors='replace')
        lines = buffer.split('\r\n')
        lines, buffer = lines[:-1], lines[-1]
        for line in lines:
            _log.info('recv: %s', line)
            yield line


def _send_message(sock, recipient, message):
    size = 400
    for line in message.splitlines():
        chunks = [line[i:i + size] for i in range(0, len(line), size)]
        for chunk in chunks:
            _send(sock, f'PRIVMSG {recipient} :{chunk}')


def _send(sock, message):
    sock.sendall(message.encode() + b'\r\n')
    _log.info('sent: %s', message)


def _parse_line(line):
    # RFC 1459 - 2.3.1
    # <message>  ::= [':' <prefix> <SPACE> ] <command> <params> <crlf>
    # <prefix>   ::= <servername> | <nick> [ '!' <user> ] [ '@' <host> ]
    # <command>  ::= <letter> { <letter> } | <number> <number> <number>
    # <SPACE>    ::= ' ' { ' ' }
    # <params>   ::= <SPACE> [ ':' <trailing> | <middle> <params> ]
    #
    # Example: :alice!Alice@user/alice PRIVMSG #hello :hello
    # Example: PING :foo.example.com
    if line[0] == ':':
        prefix, rest = line[1:].split(maxsplit=1)
    else:
        prefix, rest = None, line

    sender, command, middle, trailing = None, None, None, None

    if prefix:
        sender = prefix.split('!')[0]

    rest = rest.split(None, 1)
    command = rest[0].upper()

    if len(rest) == 2:
        params = rest[1]
        params = params.split(':', 1)
        middle = params[0].strip()
        if len(params) == 2:
            trailing = params[1].strip()

    return sender, command, middle, trailing


if __name__ == '__main__':
    main()
