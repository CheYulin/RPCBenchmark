        try {
            auto *info = context.get_context_info();
            if (!info->get_request_attachment().empty()) {
                request.attachment = info->release_request_attachment();
            }
#if DS_PRIVATE_CORO_REQUEST_MARKERS == 1
            const auto privateRequestId = private_diagnostic::NextRequestId(
                g_privateServerRequestId, private_diagnostic::kRequestIdCeiling);
            DSPrivateCoroServerRequestPhase(privateRequestId, private_diagnostic::kSubmission);
#endif
            if (StartBackgroundTask(nullptr, [this, context, request = std::move(request)
#if DS_PRIVATE_CORO_REQUEST_MARKERS == 1
                , privateRequestId
#endif
            ]() mutable {
#if DS_PRIVATE_CORO_REQUEST_MARKERS == 1
                DSPrivateCoroServerRequestPhase(privateRequestId, private_diagnostic::kLambdaEntry);
                auto reply = Run(std::move(request));
                DSPrivateCoroServerRequestPhase(privateRequestId, private_diagnostic::kReplyReady);
                Respond(std::move(context), std::move(reply));
#else
                Respond(std::move(context), Run(std::move(request)));
#endif
                Finish();
            }) == 0) {
                return;
            }
            code = K_RPC_UNAVAILABLE;
        } catch (const std::bad_alloc &) {
            code = K_OUT_OF_MEMORY;
        }
        Finish();
        Respond(std::move(context), Failure(code));
