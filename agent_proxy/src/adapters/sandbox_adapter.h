#pragma once
#include "core/model.h"
namespace ap {
class SandboxAdapter {
 public:
  virtual ~SandboxAdapter() = default;
  virtual void create(const CreateSpec&, OperationContext&, Result&) = 0;
  virtual void run(const Ref&, const Tool&, OperationContext&, Result&) = 0;
  virtual void pause(const Ref&, OperationContext&, Result&) = 0;
  virtual void resume(const ResumeSpec&, OperationContext&, Result&) = 0;
  virtual void remove(const Ref&, bool ignore_missing, OperationContext&, Result&) = 0;
  virtual void status(const Ref&, OperationContext&, Result&) = 0;
};
}  // namespace ap
