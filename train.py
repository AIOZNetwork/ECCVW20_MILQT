"""
This code is modified from Jin-Hwa Kim, Jaehyun Jun, Byoung-Tak Zhang's repository.
https://github.com/jnhwkim/ban-vqa
"""
import os
import time
import torch
import utils
import torch.nn as nn
from trainer import Trainer
warmup_updates = 4000

# Kaiming normalization initialization
def init_weights(m):
    if type(m) == nn.Linear:
        with torch.no_grad():
            torch.nn.init.kaiming_normal_(m.weight)

# VQA score computation
def compute_score_with_logits(logits, labels):
    logits = torch.max(logits, 1)[1].data # argmax
    one_hots = torch.zeros(*labels.size()).to(logits.device)
    one_hots.scatter_(1, logits.view(-1, 1), 1)
    scores = (one_hots * labels)
    return scores

# Train phase
def train(args, model, train_loader, eval_loader, num_epochs, output, opt=None, s_epoch=0):
    device = args.device
    # Scheduler learning rate
    lr_default = args.lr
    lr_decay_step = 2
    lr_decay_rate = .25
    lr_decay_epochs = range(10,20,lr_decay_step) if eval_loader is not None else range(10,20,lr_decay_step)
    gradual_warmup_steps = [0.5 * lr_default, 1.0 * lr_default, 1.5 * lr_default, 2.0 * lr_default]
    saving_epoch = 3    # Start point for model saving
    grad_clip = args.clip_norm

    utils.create_dir(output)

    # Adamax optimizer
    optim = torch.optim.Adamax(filter(lambda p: p.requires_grad, model.parameters()), lr=lr_default) \
        if opt is None else opt
    criterion = torch.nn.BCEWithLogitsLoss(reduction='sum')
    logger = utils.Logger(os.path.join(output, 'log.txt'))
    logger.write(args.__repr__())
    best_eval_score = 0

    # Kaiming normalization for initialization
    if args.weight_init == "kaiming_normal":
        model.apply(init_weights)

    utils.print_model(model, logger)
    logger.write('optim: adamax lr=%.4f, decay_step=%d, decay_rate=%.2f, grad_clip=%.2f' % \
        (lr_default, lr_decay_step, lr_decay_rate, grad_clip))

    trainer = Trainer(args, model, criterion, optim)
    update_freq = int(args.update_freq)
    wall_time_start = time.time()

    # Epoch passing in training phase
    for epoch in range(s_epoch, num_epochs):
        total_loss = 0
        train_score = 0
        train_question_type_score = 0
        total_norm = 0
        count_norm = 0
        num_updates = 0
        t = time.time()
        N = len(train_loader.dataset)
        num_batches = int(N/args.batch_size + 1)
        if epoch < len(gradual_warmup_steps):
            trainer.optimizer.param_groups[0]['lr'] = gradual_warmup_steps[epoch]
            logger.write('gradual warm up lr: %.4f' % trainer.optimizer.param_groups[0]['lr'])
        elif epoch in lr_decay_epochs:
            trainer.optimizer.param_groups[0]['lr'] *= lr_decay_rate
            logger.write('decreased lr: %.4f' % trainer.optimizer.param_groups[0]['lr'])
        else:
            logger.write('lr: %.4f' % trainer.optimizer.param_groups[0]['lr'])

        # Predicting and computing score
        for i, (v, b, q, a, qt) in enumerate(train_loader):
            v = v.to(device)
            b = b.to(device)
            q = q.to(device)
            a = a.to(device)
            qt = qt.to(device)
            sample = [v, b, q, a, qt]

            if i < num_batches - 1 and (i + 1) % update_freq > 0:
                trainer.train_step(sample, update_params=False)
            else:
                loss, grad_norm, batch_score, batch_question_type_score = trainer.train_step(sample, update_params=True)
                total_norm += grad_norm
                count_norm += 1

                total_loss += loss.item()
                train_score += batch_score
                train_question_type_score += batch_question_type_score
                num_updates += 1
                if num_updates % int(args.print_interval / update_freq) == 0:
                    print("Iter: {}, Loss {:.4f}, Norm: {:.4f}, Total norm: {:.4f}, Num updates: {}, Wall time: {:.2f}, ETA: {}".format(i + 1, total_loss / ((num_updates + 1)), grad_norm, total_norm, num_updates, time.time() - wall_time_start, utils.time_since(t, i / num_batches)))
                    if args.testing:
                        break

        total_loss /= num_updates
        train_score = 100 * train_score / (num_updates * args.batch_size)
        train_question_type_score = 100 * train_question_type_score / (num_updates * args.batch_size)

        # Evaluation
        if eval_loader is not None:
            print("Evaluating...")
            trainer.model.train(False)
            eval_score, bound, eval_question_type_score, eval_question_type_upper_bound = evaluate(model, eval_loader, args)
            trainer.model.train(True)

        logger.write('epoch %d, time: %.2f' % (epoch, time.time()-t))
        logger.write('\ttrain_loss: %.2f, norm: %.4f, score: %.2f, question type score: %.2f' % (total_loss, total_norm/count_norm, train_score, train_question_type_score))
        if eval_loader is not None:
            logger.write('\teval score: %.2f (%.2f)' % (100 * eval_score, 100 * bound))
            logger.write('\tqt_eval score: %.2f (%.2f)' % (100 * eval_question_type_score, 100 * eval_question_type_upper_bound))

        # Save per epoch
        if epoch >= saving_epoch:
            model_path = os.path.join(output, 'model_epoch%d.pth' % epoch)
            utils.save_model(model_path, model, epoch, trainer.optimizer)
            # Save best epoch
            if eval_loader is not None and eval_score > best_eval_score:
                model_path = os.path.join(output, 'model_epoch_best.pth')
                utils.save_model(model_path, model, epoch, trainer.optimizer)
                best_eval_score = eval_score

# Evaluation
def evaluate(model, dataloader, args):
    device = args.device
    score = 0
    question_type_score = 0
    upper_bound = 0
    question_type_upper_bound = 0
    num_data = 0
    with torch.no_grad():
        for v, b, q, a, qt in iter(dataloader):
            v = v.to(device)
            b = b.to(device)
            q = q.to(device)
            qt = qt.to(device)
            final_preds = None
            if args.model == "MILQT":
                _, _, combined_preds, question_type_preds, _ = model(v, b, q)   #MILQT answer and question type prediction
                final_preds = combined_preds
            # MILQT answer classification score computation
            batch_score = compute_score_with_logits(final_preds, a.to(device)).sum()
            score += batch_score
            # MILQT question-type classification score computation
            question_type_batch_score = compute_score_with_logits(question_type_preds, qt).sum()
            question_type_score += question_type_batch_score
            upper_bound += (a.max(1)[0]).sum()
            question_type_upper_bound += (qt.max(1)[0]).sum()
            num_data += final_preds.size(0)

    score = score / len(dataloader.dataset)
    upper_bound = upper_bound / len(dataloader.dataset)
    question_type_score /= len(dataloader.dataset)
    question_type_upper_bound /= len(dataloader.dataset)

    return score, upper_bound, question_type_score, question_type_upper_bound
