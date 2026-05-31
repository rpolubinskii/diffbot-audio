from __future__ import annotations

import argparse
import os
import sys

import grpc

from diffbot_audio.proto import audio_pb2


def main() -> None:
    parser = argparse.ArgumentParser(description="Call diffbot-audio Speak.")
    parser.add_argument("text")
    parser.add_argument("--host", default=os.getenv("DIFFBOT_AUDIO_CLIENT_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("DIFFBOT_AUDIO_CLIENT_PORT", "50052")))
    args = parser.parse_args()

    request = audio_pb2.SpeakRequest(text=args.text)
    with grpc.insecure_channel(f"{args.host}:{args.port}") as channel:
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
