from __future__ import annotations

import sys

import grpc

from diffbot_audio.config import ConfigError, config_arg_parser, load_config_file
from diffbot_audio.proto import audio_pb2


def main() -> None:
    parser = config_arg_parser("Call diffbot-audio Speak.")
    parser.add_argument("text")
    args = parser.parse_args()

    try:
        config = load_config_file(args.config)
    except ConfigError as exc:
        print(f"Invalid config: {exc}", file=sys.stderr)
        sys.exit(2)

    request = audio_pb2.SpeakRequest(text=args.text)
    with grpc.insecure_channel(f"{config.host}:{config.port}") as channel:
        method = channel.unary_stream(
            "/diffbot.audio.v1.AudioService/Speak",
            request_serializer=audio_pb2.SpeakRequest.SerializeToString,
            response_deserializer=audio_pb2.SpeakEvent.FromString,
        )
        failed = False
        for event in method(request):
            state = audio_pb2.SpeakState.Name(event.state)
            if event.error:
                print(f"{state}: {event.error}")
            else:
                print(state)
            failed = failed or event.state == audio_pb2.FAILED
        if failed:
            sys.exit(1)


if __name__ == "__main__":
    main()
