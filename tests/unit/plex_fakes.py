"""Plain stand-ins for plexapi collection objects, shared by the unit tests."""


class FakeLabel:
    def __init__(self, tag: str):
        self.tag = tag


class FakeColl:
    def __init__(self, title: str, tags: list[str], rating_key: int = 0):
        self.title = title
        self.labels = [FakeLabel(t) for t in tags]
        self.ratingKey = rating_key
