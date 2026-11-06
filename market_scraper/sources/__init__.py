from .base import Source, SourceContext
from .google import GoogleSource
from .meta import FacebookSource, InstagramSource
from .quora import QuoraSource
from .reddit import RedditSource
from .youtube import YouTubeSource

# Order here is the order platforms appear in the output.
SOURCE_REGISTRY: dict[str, type[Source]] = {
    cls.name: cls
    for cls in (GoogleSource, RedditSource, QuoraSource, YouTubeSource, InstagramSource, FacebookSource)
}

__all__ = ["SOURCE_REGISTRY", "Source", "SourceContext"]
