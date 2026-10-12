#pragma once
#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <deque>
#include <functional>
#include <memory>
#include <mutex>
#include <set>
#include <thread>
#include <vector>
namespace ap {
class Executor {
 public:
  Executor(size_t threads, size_t capacity) : capacity_(capacity) {
    try {
      for (size_t i = 0; i < threads; ++i)
        workers_.emplace_back([this] { loop(); });
      expiry_ = std::thread([this] { expire(); });
    } catch (...) {
      {
        std::lock_guard<std::mutex> lock(mutex_);
        stopping_ = true;
      }
      cv_.notify_all();
      for (auto& worker : workers_)
        worker.join();
      throw;
    }
  }
  ~Executor() {
    {
      std::lock_guard<std::mutex> lock(mutex_);
      stopping_ = true;
    }
    cv_.notify_all();
    expiry_.join();
    for (auto& worker : workers_)
      worker.join();
  }
  bool submit(std::function<void()> work, std::chrono::steady_clock::time_point deadline =
                                              std::chrono::steady_clock::time_point::max()) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (stopping_ || queue_.size() >= capacity_)
      return false;
    queue_.push_back({std::move(work), deadline});
    cv_.notify_all();
    return true;
  }

 private:
  void loop() {
    while (true) {
      std::function<void()> work;
      {
        std::unique_lock<std::mutex> lock(mutex_);
        cv_.wait(lock, [this] { return stopping_ || !queue_.empty(); });
        if (queue_.empty())
          return;
        work = std::move(queue_.front().work);
        queue_.pop_front();
      }
      work();
    }
  }
  void expire() {
    // Deliver expired queued requests even while all workers are blocked in HTTP.
    // Submitted work checks its deadline before doing any backend operation.
    std::unique_lock<std::mutex> lock(mutex_);
    while (!stopping_) {
      if (queue_.empty()) {
        cv_.wait(lock);
        continue;
      }
      auto it = std::min_element(queue_.begin(), queue_.end(), [](const Item& a, const Item& b) {
        return a.deadline < b.deadline;
      });
      const auto next = it->deadline;
      if (next > std::chrono::steady_clock::now()) {
        cv_.wait_until(lock, next);
        continue;
      }
      auto work = std::move(it->work);
      queue_.erase(it);
      lock.unlock();
      work();
      lock.lock();
    }
  }
  struct Item {
    std::function<void()> work;
    std::chrono::steady_clock::time_point deadline;
  };
  size_t capacity_;
  bool stopping_ = false;
  std::mutex mutex_;
  std::condition_variable cv_;
  std::deque<Item> queue_;
  std::vector<std::thread> workers_;
  std::thread expiry_;
};
class SandboxLocks {
 public:
  struct Lease {
    SandboxLocks* owner;
    std::string key;
    ~Lease() {
      std::lock_guard<std::mutex> lock(owner->mutex_);
      owner->active_.erase(key);
    }
  };
  std::shared_ptr<Lease> acquire(const std::string& key) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (!active_.insert(key).second)
      return {};
    return std::shared_ptr<Lease>(new Lease{this, key});
  }

 private:
  std::mutex mutex_;
  std::set<std::string> active_;
};
}  // namespace ap
