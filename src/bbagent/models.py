from pydantic import AwareDatetime, BaseModel


class Message(BaseModel):
    id: str
    thread_id: str
    forum_id: str
    course_id: str
    parent_id: str | None = None
    author_id: str
    author_name: str
    is_mine: bool
    body_text: str
    word_count: int
    created_at: AwareDatetime
    is_read: bool = True
    is_instructor: bool = False


class Thread(BaseModel):
    id: str
    forum_id: str
    course_id: str
    title: str
    prompt_text: str = ""
    word_limit: tuple[int, int] | None = None
    is_optional: bool = False
    root_message_id: str | None = None
    messages: list[Message] = []


class Forum(BaseModel):
    id: str
    course_id: str
    title: str
    description: str = ""
    max_chars: int | None = None
    due_at: AwareDatetime | None = None
    opens_at: AwareDatetime | None = None
    threads: list[Thread] = []
