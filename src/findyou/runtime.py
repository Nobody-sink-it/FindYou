from findyou.agent import AgentEngine
from findyou.bilibili import BilibiliAdapter
from findyou.capture import CaptureService
from findyou.config import Settings
from findyou.db import Database
from findyou.identity import IdentityService
from findyou.jobs import JobQueue
from findyou.life_memory import LifeMemoryService
from findyou.life_story import LifeStory
from findyou.models_gateway import Gateway
from findyou.services import ApplicationServices
from findyou.social import SocialStore
from findyou.storage import Storage
from findyou.worker import Worker


class Runtime:
    def __init__(self, settings: Settings, gateway=None, bili=None):
        self.settings = settings
        self.db = Database(settings)
        self.storage = Storage(settings, database=self.db)
        self.gateway = gateway or Gateway(settings)
        self.bili = bili or BilibiliAdapter(settings)
        self.services = ApplicationServices(settings, self.db, self.storage, self.gateway)
        self.queue = JobQueue(self.db, settings.job_lease_seconds)
        self.social = SocialStore(settings, self.db)
        self.identity = IdentityService(settings, self.db, self.social)
        self.capture = CaptureService(settings, self.storage, self.social, self.bili)
        self.agent = AgentEngine(settings, self.db, self.social, self.identity, self.storage,
                                 self.services, self.capture, self.bili, self.queue)
        self.life = LifeMemoryService(self)
        self.storage.life = self.life
        self.story = LifeStory(self)
        self.worker = Worker(settings, self.queue, self.storage, self.services, self.bili,
                             capture=self.capture, agent=self.agent, life=self.life, story=self.story)
