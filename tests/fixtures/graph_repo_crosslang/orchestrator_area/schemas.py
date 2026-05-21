"""Schema module used to give the cross-language fixture a real
internal import — so the imports edge resolves to a file node and the
pipeline test for ``non-http AST edges preserved alongside http`` has
something to assert against."""


class RepoRecord:
    def __init__(self, payload):
        self.payload = payload
