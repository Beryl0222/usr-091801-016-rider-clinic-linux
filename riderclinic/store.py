"""线程安全的内存存储：业务数据、独立身份库、独立健康档案与幂等缓存。"""
import threading


class Store:
    """全部状态集中于此，写操作在 lock 内串行化，保证并发改期结果确定。

    identity（身份证明核验）与 health（健康档案）为两个独立字典，分开保存；
    idempotency 以 request_id 缓存写操作结果，保证重放安全。
    """

    def __init__(self):
        self.lock = threading.RLock()
        self.seq = 0
        self.points = {}        # 服务点（静态）
        self.teams = {}         # 家庭医生团队（静态）
        self.doctors = {}       # 医生（静态）
        self.riders = {}
        self.windows = {}
        self.shifts = {}
        self.appointments = {}
        self.followups = {}
        self.referrals = {}
        self.identity = {}      # 身份证明核验结果：rider_id -> 记录（独立存储）
        self.health = {}        # 健康档案：rider_id -> [条目]（独立存储）
        self.idempotency = {}   # request_id -> 响应

    def next_seq(self):
        self.seq += 1
        return self.seq

    def new_id(self, prefix):
        return f"{prefix}-{self.next_seq():05d}"
